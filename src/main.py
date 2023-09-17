import asyncio
import io
import json
import os
import random
import time
import uuid
from pathlib import Path

import dotenv
import httpx
import regex as re
from bs4 import BeautifulSoup, Tag
from loguru import logger
from pydub import AudioSegment
from tqdm import tqdm

CONFIG_DIR = Path("config")
CREDS_JSON = CONFIG_DIR / ".creds.json"

dotenv.load_dotenv(CONFIG_DIR / ".env")
CREDS = json.load(open(CREDS_JSON))

with open(CONFIG_DIR / "voices.json") as f:
    VOICES = json.load(f)

with open(CONFIG_DIR / "global-replace.json") as f:
    GLOBAL_REPLACE = json.load(f)

ARTICLES_DIR = CONFIG_DIR / "articles"
WEB_DIR = Path("/app/web")
MANUSCRIPTS_JSON = WEB_DIR / "manuscripts.json"
AUDIO_DIR_NAME = "audio"


REFRESH_ARTICLES = os.getenv("REFRESH_ARTICLES", False)

PRE_H1_SILENCE = 0
POST_H1_SILENCE = 2
PRE_H2_SILENCE = 2
POST_H2_SILENCE = 2
PRE_P_SILENCE = 0
POST_P_SILENCE = 0.5


def _update_headers(creds: dict) -> tuple[dict, dict]:
    return creds, {
        "X-User-ID": creds["user_id"],
        "Authorization": f'Bearer {creds["api_key"]}',
        "accept": "application/json",
        "content-type": "application/json",
    }


def update_headers(current_failed: bool = False) -> tuple[dict, dict]:
    global CREDS

    if current_failed and (c := next(c for c in CREDS if c == CURRENT_CREDS)):
        c["use"] = False
        json.dump(CREDS, open(CREDS_JSON, "w"), indent=4)

    for c in CREDS:
        if c["use"]:
            logger.info(f'New creds found ({c["email"]})')
            return _update_headers(c)

    logger.error("No more useful creds, please enter new")
    CREDS.append(
        {
            "email": input("Email: "),
            "user_id": input("User ID: "),
            "api_key": input("API Key: "),
            "use": True,
        }
    )
    json.dump(CREDS, open(CREDS_JSON, "w"), indent=4)
    return _update_headers(CREDS[-1])


CURRENT_CREDS, HEADERS = update_headers()


async def urtts_v2(text: str, voice_id: str, quality: str = "premium") -> AudioSegment:
    global CURRENT_CREDS, HEADERS

    audio_r = None
    while not audio_r:
        try:
            async with httpx.AsyncClient() as client:
                tts_json = {
                    "text": text,
                    "voice": voice_id,
                    "quality": quality,
                }
                while not (
                    r := await client.post(
                        "https://play.ht/api/v2/tts",
                        headers=HEADERS,
                        timeout=300,
                        json=tts_json,
                    )
                ).is_success:
                    if r.status_code in [401, 403]:
                        logger.warning(
                            f"Credentials failed ({CURRENT_CREDS['email']}) moving on"
                        )
                        CURRENT_CREDS, HEADERS = update_headers(current_failed=True)
                    else:
                        logger.warning(f"TTS request not successful {tts_json}: {r}")
                        await asyncio.sleep(10)

                gen_id = r.json()["id"]
                fail = False
                while True:
                    r = await client.get(
                        f"https://play.ht/api/v2/tts/{gen_id}",
                        headers=HEADERS,
                        timeout=300,
                    )
                    if not r.is_success:
                        logger.error(f"TTS status request error, starting over: {r}")
                        fail = True
                        break
                    elif r.json()["output"]:
                        break

                    await asyncio.sleep(2)
                if fail:
                    continue

                gen_url = r.json()["output"]["url"]
                audio_r = await client.get(
                    gen_url,
                    timeout=300,
                )
                if not audio_r.is_success:
                    logger.warning(
                        f"Audio download failed, retrying: {audio_r} | {audio_r.text}"
                    )
                    audio_r = None

        except httpx.ReadTimeout:
            logger.warning(f"One URTTS request timed out, retrying")
        except httpx.RemoteProtocolError:
            logger.warning(f"Server disconnected during one URTTS request, retrying")

    return AudioSegment.from_mp3(io.BytesIO(audio_r.content))


def generate_voice_from_text(
    text: str,
    voice: str,
    replace: list,
    max_words: int,
) -> AudioSegment:
    for r in replace:
        pattern = re.compile(r["from"], re.IGNORECASE)
        text = pattern.sub(r["to"], text)

    texts = [text]
    if len(text.split()) > max_words:
        logger.error(
            f"Paragraph longer than max_words ({len(text.split())} > {max_words}). Paragraph split into {len(texts)} sentences."
        )
        return AudioSegment.silent(0)

    return asyncio.run(urtts_v2(text, voice))


def text_to_spans(text: str | list[str], root_dir: Path, audio_dir: Path) -> list:
    audio_dir.mkdir(parents=True, exist_ok=True)
    return [
        {
            "text": t.replace("…", "...")
            .replace("‥", "..")
            .replace(" ", "")
            .replace("–", "-")
            .strip(),
            "audio_path": str((audio_dir / f"{i:04}.mp3").absolute()),
            "audio_url": str((audio_dir / f"{i:04}.mp3").relative_to(root_dir)),
        }
        for i, t in (
            enumerate(
                re.split(
                    r"(?<=[…‥.!?\n])",
                    text.replace("...", "…").replace("..", "‥"),
                )
                if isinstance(text, str)
                else text
            )
        )
        if len(t.strip()) > 2
    ]


def generate_manuscript(
    url: str, name: str, root_dir: Path, file_name: str
) -> tuple[Path, Path, dict]:
    while True:
        try:
            soup = BeautifulSoup(
                httpx.get(url, verify=False, timeout=60).text, "html.parser"
            )
            break
        except httpx.ReadTimeout:
            logger.error(f'Could not get manuscript "{url}" retrying in 10s')
            time.sleep(10)

    page_categories = soup.find("div", {"id": "pageCategories"})

    if isinstance(page_categories, Tag):
        category_lis = [
            l.text for l in page_categories.find_all("li") if isinstance(l, Tag)
        ]
    else:
        logger.warning(
            f'"{url}" does not contain ID "pageCategories", using file stem ("{file_name}")'
        )
        category_lis = [file_name.replace("_", " ")]

    year = None
    season = None
    title = None
    for category in category_lis:
        if "YE" in category:
            year, season = category.split("YE")
            year = year.strip() + "YE"
            season = season.strip()
            title = (
                f"{season}, {year}. {name}"
                if "YE " not in name
                else name.replace("YE ", "YE. ")
            )
            break
        else:
            year = "other"
            season = category
            title = name

    if not year or not season:
        year = "other"
        season = "unknown"

    res_dir = root_dir / year / season / name
    res_dir.mkdir(parents=True, exist_ok=True)

    audio_dir = res_dir / AUDIO_DIR_NAME
    audio_dir.mkdir(parents=True, exist_ok=True)

    content = soup.find("div", {"id": "mw-content-text"})

    if not isinstance(content, Tag):
        raise TypeError(f'Soup does not contain ID "mw-content-text": {content}')

    toc = content.find("div", {"id": "toc"})
    if isinstance(toc, Tag):
        toc.decompose()  # remove Table of Content

    for child in content.find_all("div"):
        child.decompose()
    for child in content.find_all("sup"):
        child.decompose()
    for child in content.find_all("table"):
        child.decompose()

    (audio_dir / f"{0:04}").mkdir(parents=True, exist_ok=True)
    manuscript = {
        "name": name,
        "year": year,
        "season": season,
        "url": url,
        "sections": [
            {
                "section_type": "h1",
                "spans": [
                    {
                        "text": title,
                        "audio_path": str(
                            (audio_dir / f"{0:04}" / f"{0:04}.mp3").absolute()
                        ),
                        "audio_url": str(
                            (audio_dir / f"{0:04}" / f"{0:04}.mp3").relative_to(
                                root_dir
                            )
                        ),
                    }
                ],
            },
            *[
                {
                    "section_type": child.name,
                    "spans": text_to_spans(
                        child.text
                        if child.name != "ul"
                        else [c.text for c in child.findChildren(recursive=False)],
                        root_dir,
                        audio_dir / f"{i+1:04}",
                    ),
                }
                for i, child in enumerate(content.findChildren(recursive=False))
            ],
        ],
        "outro": {
            "path": str((audio_dir / "outro.mp3").absolute()),
            "url": str((audio_dir / "outro.mp3").relative_to(root_dir)),
        },
    }

    return res_dir, audio_dir, manuscript


def generate_audio(
    manuscript: dict,
    manuscript_i: int,
    manuscript_total: int,
    task: str,
) -> None:
    voice = random.choice([v for v in VOICES if v["use"]])

    logger.info(
        f'Chose voice "{voice["name"]}" for "{manuscript["name"]} | {manuscript["season"]} | {manuscript["year"]}"'
    )

    max_words = voice["max_words"] if "max_words" in voice else float("inf")

    for i, section in enumerate(manuscript["sections"]):
        for span in tqdm(
            section["spans"],
            desc=f'{task} {manuscript_i+1}/{manuscript_total} "{manuscript["name"]}": {i}/{len(manuscript["sections"])-1}',
        ):
            generate_voice_from_text(
                span["text"],
                voice["id"],
                GLOBAL_REPLACE + voice["replace"],
                max_words,
            ).export(span["audio_path"])

    generate_voice_from_text(
        f'This article was read aloud by the artificial voice, "{voice["name"]}". All content of this recording is the original work of Profound Decisions and can be found on the Empire wikipedia. Thank you for listening.',
        voice["id"],
        GLOBAL_REPLACE + voice["replace"],
        max_words,
    ).export(manuscript["outro"]["path"])
    logger.info(
        f'TTS audio segments generated for article "{manuscript["name"]}": {i}/{len(manuscript["sections"])-1}'
    )


def article_url_to_name(url: str) -> str:
    return url.rsplit("/", 1)[-1].replace("_", " ").strip()


def main() -> None:
    logger.info("Starting main loop")
    if not MANUSCRIPTS_JSON.exists():
        with open(MANUSCRIPTS_JSON, "w") as f:
            f.write("{}")

    while True:
        article_urls: set[tuple[str, str]] = set()
        for article_file in ARTICLES_DIR.iterdir():
            with open(article_file) as f:
                article_urls.update((article_file.stem, url) for url in json.load(f))

        with open(MANUSCRIPTS_JSON) as f:
            manuscripts = json.load(f)

        missing_manuscripts = []
        updated_manuscripts = []
        for file_name, article_url in list(article_urls):
            name = article_url_to_name(article_url)
            try:
                res_dir, audio_dir, manuscript = generate_manuscript(
                    article_url, name, WEB_DIR, file_name
                )

                if manuscript["name"] in manuscripts:
                    existing_manuscript = manuscripts[manuscript["name"]]

                    if len(manuscript["sections"]) + 1 > len(list(audio_dir.iterdir())):
                        logger.warning(
                            f'Article "{manuscript["name"]}" ({article_url}) has fewer generated files ({len(list(audio_dir.iterdir()))}) than needed ({len(manuscript["sections"]) + 1}), regenerating files'
                        )
                        updated_manuscripts.append(manuscript)
                    if REFRESH_ARTICLES and manuscript != existing_manuscript:
                        logger.info(
                            f'Article "{manuscript["name"]}" ({article_url}) changed, adding to updated manuscripts'
                        )
                        updated_manuscripts.append(manuscript)
                else:
                    logger.info(
                        f'Article "{manuscript["name"]}" ({article_url}) not yet generated, added to new manuscripts'
                    )
                    missing_manuscripts.append(manuscript)
            except httpx.ConnectError as e:
                logger.warning(f'Could not GET article "{article_url}": {e}')

        if missing_manuscripts:
            logger.info(f"Generating {len(missing_manuscripts)} missing manuscripts")
            for i, manuscript in enumerate(missing_manuscripts):
                generate_audio(
                    manuscript, i, len(missing_manuscripts), "Generating manuscripts"
                )

                manuscripts[manuscript["name"]] = manuscript

                # Sort by key (article name)
                manuscripts = dict(sorted(manuscripts.items()))

                with open(MANUSCRIPTS_JSON, "w") as f:
                    json.dump(manuscripts, f, indent=4)

        if updated_manuscripts:
            logger.info(f"Generating {len(updated_manuscripts)} updated manuscripts")
            for i, manuscript in enumerate(updated_manuscripts):
                generate_audio(
                    manuscript, i, len(updated_manuscripts), "Updating manuscripts"
                )

                manuscripts[manuscript["name"]] = manuscript

                # Sort by key (article name)
                manuscripts = dict(sorted(manuscripts.items()))

                with open(MANUSCRIPTS_JSON, "w") as f:
                    json.dump(manuscripts, f, indent=4)

        time.sleep(60 * 60)  # check every hour


if __name__ == "__main__":
    main()
