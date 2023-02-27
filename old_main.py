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
from tqdm import tqdm

dotenv.load_dotenv("./config/.env")

with open("./config/voices.json") as f:
    VOICES = json.load(f)

ARTICLES_JSON = "./config/articles.json"

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

TMP_WAV = Path("tmp.wav")


def urtts(to_say: str, voice: str, verbose: bool = False) -> AudioSegment:
    start = time.time()

    convert_r = None
    while not convert_r:
        request_json = {"voice": voice, "content": [to_say]}
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
            ).json()
        except httpx.ReadTimeout:
            logger.warning(f"URTTS articleStatus request timed out, retrying")

    assert len(result_r["audioUrl"]) == 1, request_json

    while (
        d := httpx.get(
            result_r["audioUrl"][0],
        )
    ).status_code == 403:
        time.sleep(0.5)

    with open(TMP_WAV, "wb") as f:
        f.write(d.content)

    if verbose:
        logger.info(f"Got URTTS in {time.time() - start}s")

    return AudioSegment.from_wav(TMP_WAV)


def generate_manus_from_text(
    markdown_manuscript: str, html_manuscript: str, text: str, text_type: str
) -> tuple[str, str]:
    if text_type == "h1":
        markdown_manuscript += f"\n# {text}:\n"
        html_manuscript += f"\n<h1>{text}</h1>"
    elif text_type == "h2":
        markdown_manuscript += f"\n## {text}:\n"
        html_manuscript += f"\n<h2>{text}</h2>"
    elif text_type == "h3":
        markdown_manuscript += f"\n### {text}:\n"
        html_manuscript += f"\n<h3>{text}</h3>"
    elif text_type == "p":
        markdown_manuscript += f"{text}\n"
        html_manuscript += f"\n<p>{text}</p>"
    else:
        raise NameError(f'Unknown element type "{text_type}"')
    return (markdown_manuscript, html_manuscript)


def generate_voice_from_text(
    text: str,
    text_type: str,
    voice: str,
    replace: list,
    max_words: int,
) -> AudioSegment:
    if text_type == "h1":
        audio = AudioSegment.silent(duration=PRE_H1_SILENCE * 1000)
    elif text_type == "h2" or text_type == "h3":
        audio = AudioSegment.silent(duration=PRE_H2_SILENCE * 1000)
    elif text_type == "p":
        audio = AudioSegment.silent(duration=PRE_P_SILENCE * 1000)
    else:
        raise NameError(f'Unknown element type "{text_type}"')

    for r in replace:
        pattern = re.compile(r["from"], re.IGNORECASE)
        text = pattern.sub(r["to"], text)

    texts = [text]
    if len(text.split()) > max_words:
        texts = [t.strip() for t in re.split(r"[.!?]", text) if t.strip()]
        logger.warning(
            f"Paragraph longer than max_words ({len(text.split())} > {max_words}). Paragraph split into {len(texts)} sentences."
        )
    for t in texts:
        audio += urtts(t, voice)

    if text_type == "h1":
        audio += AudioSegment.silent(duration=int(POST_H1_SILENCE * 1000))
    elif text_type == "h2" or text_type == "h3":
        audio += AudioSegment.silent(duration=int(POST_H2_SILENCE * 1000))
    elif text_type == "p":
        audio += AudioSegment.silent(duration=int(POST_P_SILENCE * 1000))
    else:
        raise NameError(f'Unknown element type "{text_type}"')

    return audio


def get_manuscript(url: str, root_dir: str = "/data") -> tuple[str, Path, str, str]:
    name = url.rsplit("/", 1)[-1].replace("_", " ").strip()

    soup = BeautifulSoup(httpx.get(url).text, "html.parser")

    page_categories = soup.find("div", {"id": "pageCategories"})

    if not isinstance(page_categories, Tag):
        raise TypeError(f'Soup does not contain ID "pageCategories": {page_categories}')

    for child in page_categories.findChildren(recursive=True):
        if "YE" in child.text:
            year, season = child.text.split("YE")
            year = year.strip() + "YE"
            season = season.strip()

    res_dir = Path(root_dir) / year / season / name
    res_dir.mkdir(parents=True, exist_ok=True)

    content = soup.find("div", {"id": "mw-content-text"})

    if not isinstance(content, Tag):
        raise TypeError(f'Soup does not contain ID "mw-content-text": {content}')

    toc = content.find("div", {"id": "toc"})

    if not isinstance(toc, Tag):
        raise TypeError(f'Soup does not contain ID "toc": {toc}')

    toc.decompose()  # remove Table of Content
    for child in content.find_all("div"):
        child.decompose()
    for child in content.find_all("sup"):
        child.decompose()
    for child in content.find_all("ul"):
        child.decompose()

    markdown_manuscript = ""
    html_manuscript = ""

    title = (f"{season}, {year}. {name}" if "YE " not in name else name).replace(
        "YE", " Year of the Empire"
    )

    markdown_manuscript, html_manuscript = generate_manus_from_text(
        markdown_manuscript, html_manuscript, title, "h1"
    )
    for child in content.findChildren(recursive=False):
        markdown_manuscript, html_manuscript = generate_manus_from_text(
            markdown_manuscript,
            html_manuscript,
            child.text.replace("–", "-").replace(" ", ""),
            child.name,
        )

    markdown_manuscript += "\n---\n"
    html_manuscript += "<hr>\n"

    markdown_manuscript += f"\n[{url}]({url})"
    html_manuscript += f'\n<a href="{url}">{url}</a>'

    return (
        name,
        res_dir,
        markdown_manuscript.strip(),
        html_manuscript.strip(),
    )


def parse_manuscript(name: str, result_dir: Path, html_manuscript: str) -> None:
    voice = random.choice([v for v in VOICES if v["use"]])

    logger.debug(f'Chose voice "{voice["name"]}"')
    logger.debug(f'Saving to "{result_dir}"')

    audio = AudioSegment.silent(duration=0)

    max_words = voice["max_words"] if "max_words" in voice else float("inf")

    soup = BeautifulSoup(html_manuscript, "html.parser").find("body")

    assert isinstance(soup, Tag)

    hr_tags = soup.find_all("hr")
    if isinstance(hr_tags, Tag):
        for child in hr_tags:
            child.decompose()

    a_tags = soup.find_all("a")
    if isinstance(a_tags, Tag):
        for child in a_tags:
            child.decompose()

    for child in tqdm(
        soup.findChildren(recursive=False),
        desc="Generating TTS audio from page segments",
    ):
        audio += generate_voice_from_text(
            child.text,
            child.name,
            voice["name"],
            voice["replace"],
            max_words,
        )

    audio += AudioSegment.silent(duration=4 * 1000)

    audio += generate_voice_from_text(
        f'This article was read aloud by the artificial voice, "{voice["name"]}". All content of this recording is the original work of Profound Decisions and can be found on the Empire wikipedia. Thank you for listening.',
        "p",
        voice["name"],
        voice["replace"],
        max_words,
    )

    TMP_WAV.unlink(missing_ok=True)

    waw_file = result_dir / f"{name}.wav"
    audio.export(waw_file.absolute(), format="wav")

    mp3_file = result_dir / f"{name}.mp3"
    audio.export(mp3_file.absolute(), format="mp3")


def main() -> None:
    articles = []
    logger.info("Starting main loop")
    while True:
        with open(ARTICLES_JSON) as f:
            articles = json.load(f)

        for article in articles:
            name, RES_DIR, markdown_manuscript, html_manuscript = get_manuscript(
                article
            )
            markdown_manuscript_file = RES_DIR / f"{name}.md"
            html_manuscript_file = RES_DIR / f"{name}.html"
            update_files = False
            if markdown_manuscript_file.exists() and html_manuscript_file.exists():
                with open(markdown_manuscript_file) as f:
                    existing_markdown_manuscript = f.read()
                with open(html_manuscript_file) as f:
                    existing_html_manuscript = f.read()

                if (
                    markdown_manuscript != existing_markdown_manuscript
                    or html_manuscript != existing_html_manuscript
                ):
                    logger.warning(f'"{article}" changed, updating files')
                    update_files = True
            else:
                logger.warning(f'"{article}" not yet generated, creating files')
                update_files = True

            if update_files:
                with open(markdown_manuscript_file, "w") as f:
                    f.write(markdown_manuscript)
                with open(html_manuscript_file, "w") as f:
                    f.write(html_manuscript)
                parse_manuscript(name, RES_DIR, html_manuscript)

        time.sleep(60 * 60)  # check every hour


if __name__ == "__main__":
    main()
