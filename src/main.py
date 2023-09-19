import asyncio
import io
import json
import multiprocessing
import os
import random
import time
import uuid
from pathlib import Path
from typing import Any

import dotenv
import httpx
import pymongo
import regex as re
from bs4 import BeautifulSoup, Tag
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydub import AudioSegment
from starlette.responses import FileResponse
from tqdm import tqdm

CONFIG_DIR = Path("config")
CREDS_JSON = CONFIG_DIR / ".creds.json"

dotenv.load_dotenv(CONFIG_DIR / ".env")

with open(CONFIG_DIR / "voices.json") as f:
    VOICES = json.load(f)

with open(CONFIG_DIR / "global-replace.json") as f:
    GLOBAL_REPLACE = json.load(f)

ARTICLES_DIR = CONFIG_DIR / "articles"
WEB_DIR = Path("/app/web")
DB_DIR = WEB_DIR / "db"
MANUSCRIPTS_JSON = os.environ.get("MANUSCRIPTS_JSON")
MONGODB_DOMAIN = os.environ.get("MONGODB_DOMAIN", default="localhost")
AUDIO_DIR_NAME = "audio"
WIKI_URL = "https://www.profounddecisions.co.uk/empire-wiki"

REFRESH_ARTICLES = os.getenv("REFRESH_ARTICLES", False)

PRE_H1_SILENCE = 0
POST_H1_SILENCE = 2
PRE_H2_SILENCE = 2
POST_H2_SILENCE = 2
PRE_P_SILENCE = 0
POST_P_SILENCE = 0.5

app = FastAPI()
mongodb_client: pymongo.MongoClient = pymongo.MongoClient(MONGODB_DOMAIN, 27017)
DB = mongodb_client["database"]
COLLECTION = DB["manuscripts"]

if MANUSCRIPTS_JSON:
    with open(MANUSCRIPTS_JSON) as f:
        manuscripts = json.load(f)

    to_insert = [
        {"_id": key.replace(" ", "_"), **value} for key, value in manuscripts.items()
    ]
    COLLECTION.insert_many(to_insert)
    logger.info(f"Inserted {len(to_insert)} manuscripts into mongodb")


def _update_headers(creds: dict) -> tuple[dict, dict]:
    return creds, {
        "X-User-ID": creds["user_id"],
        "Authorization": f'Bearer {creds["api_key"]}',
        "accept": "application/json",
        "content-type": "application/json",
    }


async def update_headers(current_creds: dict | None = None) -> tuple[dict, dict]:
    creds = json.load(open(CREDS_JSON))

    if current_creds and (
        c := next(c for c in creds if c["email"] == current_creds["email"])
    ):
        c["use"] = False
        json.dump(creds, open(CREDS_JSON, "w"), indent=4)

    while True:
        creds = json.load(open(CREDS_JSON))
        useful_creds = [c for c in creds if c["use"]]
        if useful_creds:
            c = useful_creds[0]
            logger.info(
                f'New creds found "{c["email"]}" ({len(useful_creds)-1} more left)'
            )
            return _update_headers(c)

        logger.error("No more useful creds, please update file - checing in 10 min")
        await asyncio.sleep(10 * 60)


CURRENT_CREDS = None
HEADERS = None


async def urtts_v2(text: str, voice_id: str, quality: str = "premium") -> AudioSegment:
    global CURRENT_CREDS, HEADERS
    if not CURRENT_CREDS or not HEADERS:
        CURRENT_CREDS, HEADERS = await update_headers()

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
                        CURRENT_CREDS, HEADERS = await update_headers(CURRENT_CREDS)
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
            "audio_url": "/" + str((audio_dir / f"{i:04}.mp3").relative_to(root_dir)),
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


def generate_error_manuscript(article_id: str) -> dict:
    article_url = f"{WIKI_URL}/{article_id}"

    res_dir = DB_DIR / article_id
    res_dir.mkdir(parents=True, exist_ok=True)

    audio_dir = res_dir / AUDIO_DIR_NAME
    audio_dir.mkdir(parents=True, exist_ok=True)

    audio_dir0 = audio_dir / f"{0:04}"
    audio_dir0.mkdir(parents=True, exist_ok=True)
    audio_dir1 = audio_dir / f"{1:04}"
    audio_dir1.mkdir(parents=True, exist_ok=True)

    return {
        "title": article_id.replace("_", " "),
        "url": article_url,
        "sections": [
            {
                "section_type": "h1",
                "spans": [
                    {
                        "text": article_id.replace("_", " "),
                        "audio_path": str((audio_dir0 / f"{0:04}.mp3").absolute()),
                        "audio_url": "/"
                        + str((audio_dir0 / f"{0:04}.mp3").relative_to(WEB_DIR)),
                    }
                ],
            },
            {
                "section_type": "p",
                "spans": [
                    {
                        "text": "This article could not be processed by the system.",
                        "audio_path": str((audio_dir1 / f"{0:04}.mp3").absolute()),
                        "audio_url": "/"
                        + str((audio_dir1 / f"{0:04}.mp3").relative_to(WEB_DIR)),
                    },
                    {
                        "text": f"This could either be because the article ({article_url}) does not exist or because an error happend while downloading it.",
                        "audio_path": str(((audio_dir1 / f"{1:04}.mp3").absolute())),
                        "audio_url": "/"
                        + str((audio_dir1 / f"{1:04}.mp3").relative_to(WEB_DIR)),
                    },
                    {
                        "text": "The system will regularly retry to process the article in case the problem is only temporary.",
                        "audio_path": str((audio_dir1 / f"{2:04}.mp3").absolute()),
                        "audio_url": "/"
                        + str((audio_dir1 / f"{2:04}.mp3").relative_to(WEB_DIR)),
                    },
                ],
            },
        ],
        "outro": {
            "audio_path": str((audio_dir / "outro.mp3").absolute()),
            "audio_url": "/" + str((audio_dir / "outro.mp3").relative_to(WEB_DIR)),
        },
    }


def generate_manuscript(article_id: str, res_dir: Path, audio_dir: Path) -> dict:
    url = f"{WIKI_URL}/{article_id}"
    try:
        response = httpx.get(url, verify=False, timeout=60)
    except Exception as e:
        logger.error(f'Could not get article "{url}": {e}')
        return generate_error_manuscript(article_id)

    if not response.is_success:
        logger.error(f'Could not get article "{url}": {response}')
        return generate_error_manuscript(article_id)

    soup = BeautifulSoup(response.text, "html.parser")

    content = soup.find("div", {"id": "mw-content-text"})

    if not isinstance(content, Tag):
        logger.error(f'Soup for "{url}" does not contain ID "mw-content-text"')
        return generate_error_manuscript(article_id)

    title_tag = soup.find("h1")
    if not isinstance(title_tag, Tag):
        logger.error(f'Soup for "{url}" does not contain h1 header')
        return generate_error_manuscript(article_id)
    title = title_tag.text.strip()

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

    return {
        "title": title,
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
                        "audio_url": "/"
                        + str(
                            (audio_dir / f"{0:04}" / f"{0:04}.mp3").relative_to(WEB_DIR)
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
                        WEB_DIR,
                        audio_dir / f"{i+1:04}",
                    ),
                }
                for i, child in enumerate(content.findChildren(recursive=False))
            ],
        ],
        "outro": {
            "audio_path": str((audio_dir / "outro.mp3").absolute()),
            "audio_url": "/" + str((audio_dir / "outro.mp3").relative_to(WEB_DIR)),
        },
    }


def generate_audio(manuscript: dict, task: str) -> None:
    voice = random.choice([v for v in VOICES if v["use"]])

    logger.info(f'Chose voice "{voice["name"]}" for "{manuscript["title"]}"')

    max_words = voice["max_words"] if "max_words" in voice else float("inf")

    for i, section in enumerate(manuscript["sections"]):
        for span in tqdm(
            section["spans"],
            desc=f'{task} "{manuscript["title"]}": {i}/{len(manuscript["sections"])-1}',
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
    ).export(manuscript["outro"]["audio_path"])
    logger.info(
        f'TTS audio segments generated for article "{manuscript["title"]}": {i}/{len(manuscript["sections"])-1}'
    )


def insert_or_replace(manuscript: dict) -> None:
    try:
        COLLECTION.insert_one(manuscript)
    except pymongo.errors.DuplicateKeyError:
        COLLECTION.replace_one({"_id": manuscript["_id"]}, manuscript)


def article_processor(queue: multiprocessing.Queue) -> None:
    while True:
        article_id = queue.get(block=True, timeout=None)
        logger.info(
            f'Processing "{article_id}" ({queue.qsize()} articles left in queue)'
        )

        res_dir = DB_DIR / article_id
        res_dir.mkdir(parents=True, exist_ok=True)

        audio_dir = res_dir / AUDIO_DIR_NAME
        audio_dir.mkdir(parents=True, exist_ok=True)

        try:
            manuscript = {
                "_id": article_id,
                **generate_manuscript(article_id, res_dir, audio_dir),
            }

            existing_manuscript = COLLECTION.find_one({"_id": (article_id)})
            if existing_manuscript is not None:
                if REFRESH_ARTICLES and manuscript != existing_manuscript:
                    logger.info(
                        f'Article "{manuscript["title"]}" ({manuscript["url"]}) changed, updating manuscript'
                    )
                    generate_audio(manuscript, "Updating manuscript")
                    insert_or_replace(manuscript)
                if len(manuscript["sections"]) + 1 > len(list(audio_dir.iterdir())):
                    logger.warning(
                        f'Article "{manuscript["title"]}" ({manuscript["url"]}) has fewer generated files ({len(list(audio_dir.iterdir()))}) than needed ({len(manuscript["sections"]) + 1}), regenerating files'
                    )
                    generate_audio(manuscript, "Updating manuscript")
                    insert_or_replace(manuscript)
            else:
                logger.info(
                    f'Article "{manuscript["title"]}" ({manuscript["url"]}) not yet generated, generating manuscript'
                )
                generate_audio(manuscript, "Generating manuscript")
                insert_or_replace(manuscript)
        except httpx.ConnectError as e:
            logger.warning(f'Could not GET article "{article_id}": {e}')


article_queue: multiprocessing.Queue = multiprocessing.Queue()
multiprocessing.Process(target=article_processor, args=(article_queue,)).start()


@app.get("/empire-wiki/{article:path}")
def index(article: str) -> FileResponse:
    return FileResponse("/app/web/index.html")


@app.get("/api/manuscript/{article_id:path}")
def manuscript(article_id: str) -> Any:
    article_id = article_id.replace(" ", "_")
    manuscript = COLLECTION.find_one({"_id": article_id})
    article_queue.put(article_id)
    if manuscript is not None:
        return manuscript
    else:
        insert_or_replace(
            {
                "_id": article_id,
                "title": article_id,
                "url": f"{WIKI_URL}/{article_id}",
                "sections": [
                    {
                        "section_type": "h1",
                        "spans": [
                            {
                                "text": article_id,
                                "audio_path": "",
                                "audio_url": "",
                            }
                        ],
                    },
                    {
                        "section_type": "p",
                        "spans": [
                            {
                                "text": "Generating...",
                                "audio_path": "",
                                "audio_url": "",
                            }
                        ],
                    },
                    {
                        "section_type": "p",
                        "spans": [
                            {
                                "text": "The system is currently processing this article."
                            },
                            {
                                "text": "This will take anywhere from a couple of minutes to hours, depending on the article and how many articles are ahead of this one in the queue."
                            },
                            {
                                "text": "You are welcome to come back later to check but unfortunately the system is not smart enough to give you an estimate."
                            },
                        ],
                    },
                ],
            }
        )
        return {
            "title": article_id,
            "url": f"{WIKI_URL}/{article_id}",
            "sections": [
                {
                    "section_type": "h1",
                    "spans": [
                        {
                            "text": "New Article!",
                            "audio_path": "",
                            "audio_url": "",
                        }
                    ],
                },
                {
                    "section_type": "p",
                    "spans": [
                        {
                            "text": "Congratulations! You are the first to visit this article!",
                            "audio_path": "",
                            "audio_url": "",
                        }
                    ],
                },
                {
                    "section_type": "p",
                    "spans": [
                        {
                            "text": "Unfortunately, this means that the system now have to generate this article.",
                            "audio_path": "",
                            "audio_url": "",
                        },
                        {
                            "text": "This it will take anywhere from a couple of minutes to hours, depending on the article and how many articles are ahead of this one in the queue.",
                            "audio_path": "",
                            "audio_url": "",
                        },
                        {
                            "text": "You are welcome to come back later to check again but unfortunately the system is not smart enough to give you an estimate.",
                            "audio_path": "",
                            "audio_url": "",
                        },
                    ],
                },
            ],
        }


app.mount("/", StaticFiles(directory="/app/web", html=True), name="Web")
