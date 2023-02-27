import json
import os
import random
import re
import time
from pathlib import Path

import dotenv
import httpx
from bs4 import BeautifulSoup, Tag
from loguru import logger
from pydub import AudioSegment

CONFIG_DIR = Path("config")

dotenv.load_dotenv(CONFIG_DIR / ".env")

with open(CONFIG_DIR / "voices.json") as f:
    VOICES = json.load(f)

ARTICLES_DIR = CONFIG_DIR / "articles"
WEB_DIR = Path("/app/web")
MANUSCRIPTS_JSON = WEB_DIR / "manuscripts.json"

API_KEY = os.environ["URTTS_API_KEY"]
USER_ID = os.environ["URTTS_USER_ID"]
PARROT_USERNAME = os.environ["PARROT_USERNAME"]
PARROT_PASSWORD = os.environ["PARROT_PASSWORD"]

PRE_H1_SILENCE = 0
POST_H1_SILENCE = 2
PRE_H2_SILENCE = 2
POST_H2_SILENCE = 2
PRE_P_SILENCE = 0
POST_P_SILENCE = 0.5

TMP_WAV = WEB_DIR / "tmp.wav"


def urtts(to_say: str, voice: str, verbose: bool = False) -> AudioSegment:
    start = time.time()
    audio_r = None
    request_json = {"voice": voice, "content": [to_say]}
    while not audio_r:
        try:
            convert_r = None
            while not convert_r:
                try:
                    convert_r = httpx.post(
                        "https://play.ht/api/v1/convert",
                        headers={
                            "X-User-ID": USER_ID,
                            "Authorization": API_KEY,
                        },
                        json=request_json,
                    )
                except httpx.ReadTimeout:
                    logger.warning(f"URTTS convert request timed out, retrying")
                    continue

                if str(convert_r.status_code)[0] != "2":
                    logger.warning(
                        f"URTTS convert request failed, retrying in 10s: {request_json} -> {convert_r.status_code} - {convert_r.text}"
                    )
                    convert_r = None
                    time.sleep(10)

            result_r = None
            while not result_r:
                try:
                    result_r = httpx.get(
                        f"https://play.ht/api/v1/articleStatus?transcriptionId={convert_r.json()['transcriptionId']}&ultra=true",
                        headers={
                            "X-User-ID": USER_ID,
                            "Authorization": API_KEY,
                        },
                    )
                except httpx.ReadTimeout:
                    logger.warning(f"URTTS articleStatus request timed out, retrying")
                    continue

                if "audioUrl" not in result_r.json():
                    logger.warning(
                        f"URTTS articleStatus request failed, retrying in 10s: {result_r.status_code} - {result_r.text}"
                    )
                    result_r = None
                    time.sleep(10)

            audioUrl = result_r.json()["audioUrl"]

            assert len(audioUrl) == 1, request_json

            MAX_RETRIES = 60
            current_retries = 0
            while (audio_r := httpx.get(audioUrl[0])).status_code == 403:
                if current_retries < MAX_RETRIES:
                    current_retries += 1
                    time.sleep(1)
                else:
                    logger.warning(
                        f'Max retries ({MAX_RETRIES}) exceeded while trying to get audioURL ({audioUrl[0]}) with request "{request_json}", starting over'
                    )
                    audio_r = None
                    break

        except httpx.ConnectTimeout:
            audio_r = None
            logger.warning(f"Could not connect to URTTS, retrying")

    with open(TMP_WAV, "wb") as f:
        f.write(audio_r.content)

    if verbose:
        logger.info(f"Got URTTS in {time.time() - start}s")

    return AudioSegment.from_wav(TMP_WAV)


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
        raise Exception(
            f"Paragraph longer than max_words ({len(text.split())} > {max_words}). Paragraph split into {len(texts)} sentences."
        )

    return urtts(text, voice)


def text_to_spans(text: str, root_dir: Path, audio_dir: Path) -> list:
    audio_dir.mkdir(parents=True, exist_ok=True)
    return [
        {
            "text": t.strip().replace("…", "...").replace("‥", ".."),
            "audio_path": str((audio_dir / f"{i:04}.mp3").absolute()),
            "audio_url": str((audio_dir / f"{i:04}.mp3").relative_to(root_dir)),
        }
        for i, t in enumerate(
            re.split(
                r"(?<=[…‥.!?])",
                text.replace("–", "-")
                .replace(" ", "")
                .replace("...", "…")
                .replace("..", "‥"),
            )
        )
        if t.strip()
    ]


def generate_manuscript(url: str, name: str, root_dir: Path) -> tuple[Path, dict]:
    soup = BeautifulSoup(httpx.get(url).text, "html.parser")

    page_categories = soup.find("div", {"id": "pageCategories"})

    if not isinstance(page_categories, Tag):
        raise TypeError(f'Soup does not contain ID "pageCategories": {url}')

    for child in page_categories.findChildren(recursive=True):
        if "YE" in child.text:
            year, season = child.text.split("YE")
            year = year.strip() + "YE"
            season = season.strip()

    res_dir = root_dir / year / season / name
    res_dir.mkdir(parents=True, exist_ok=True)

    audio_dir = res_dir / "audio"
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
    for child in content.find_all("ul"):
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
                        "text": f"{season}, {year}. {name}"
                        if "YE " not in name
                        else name.replace("YE ", "YE. "),
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
                        child.text, root_dir, audio_dir / f"{i+1:04}"
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

    return res_dir, manuscript


def generate_audio(manuscript: dict) -> None:
    voice = random.choice([v for v in VOICES if v["use"]])

    logger.info(f'Chose voice "{voice["name"]}" for "{manuscript["name"]}"')

    max_words = voice["max_words"] if "max_words" in voice else float("inf")

    for i, section in enumerate(manuscript["sections"]):
        logger.info(
            f'Generating TTS audio segments for article "{manuscript["name"]}": {i}/{len(manuscript["sections"])-1}'
        )
        for span in section["spans"]:
            generate_voice_from_text(
                span["text"],
                voice["name"],
                voice["replace"],
                max_words,
            ).export(span["audio_path"])

    generate_voice_from_text(
        f'This article was read aloud by the artificial voice, "{voice["name"]}". All content of this recording is the original work of Profound Decisions and can be found on the Empire wikipedia. Thank you for listening.',
        voice["name"],
        voice["replace"],
        max_words,
    ).export(manuscript["outro"]["path"])

    TMP_WAV.unlink(missing_ok=True)


def main() -> None:
    logger.info("Starting main loop")
    if not MANUSCRIPTS_JSON.exists():
        with open(MANUSCRIPTS_JSON, "w") as f:
            f.write("{}")
    while True:
        article_urls = set()
        for article_file in ARTICLES_DIR.iterdir():
            with open(article_file) as f:
                article_urls.update(json.load(f))

        with open(MANUSCRIPTS_JSON) as f:
            manuscripts = json.load(f)

        for article_url in list(article_urls):
            name = article_url.rsplit("/", 1)[-1].replace("_", " ").strip()
            res_dir, manuscript = generate_manuscript(article_url, name, WEB_DIR)

            update_files = False
            if manuscript["name"] in manuscripts:
                existing_manuscript = manuscripts[manuscript["name"]]

                if manuscript != existing_manuscript:
                    logger.warning(
                        f'Article "{manuscript["name"]}" ({article_url}) changed, updating files'
                    )
                    update_files = True
            else:
                logger.warning(
                    f'Article "{manuscript["name"]}" ({article_url}) not yet generated, creating files'
                )
                update_files = True

            if update_files:
                generate_audio(manuscript)

                manuscripts[manuscript["name"]] = manuscript
                with open(MANUSCRIPTS_JSON, "w") as f:
                    json.dump(manuscripts, f, indent=4)

        time.sleep(60 * 60)  # check every hour


if __name__ == "__main__":
    main()
