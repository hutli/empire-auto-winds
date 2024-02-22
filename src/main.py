import asyncio
import base64
import dataclasses
import io
import json
import multiprocessing
import os
import random
import time
import uuid
from pathlib import Path
from typing import Any, Generator

import dotenv
import httpx
import pydub
import pymongo
import regex as re
import websockets
from bs4 import BeautifulSoup, Tag
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic.dataclasses import dataclass
from starlette.responses import FileResponse

CONFIG_DIR = Path("config")

dotenv.load_dotenv(CONFIG_DIR / ".env")

ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]

with open(CONFIG_DIR / "voices.json") as f:
    VOICES = json.load(f)

GLOBAL_REPLACE = [
    ("sumaah", "Suhmah"),
    ("jotun", "Jotoon"),
    ("vallorn", "Valorn"),
    ("druj", "Drooge"),
    ("feni", "Fenni"),
    ("in-character", "incharacter"),
    ("temeschwar", "Temmeschwar"),
    ("sermersuaq", "semmersuak"),
    ("thule", "thool"),
    ("egregore", "egrigore"),
    ("(?<=\\d{3})YE", " Year of the Empire"),
    ("(?<=^|\\s|\\n)wiki(?=[[:punct:]]|$|\\n|\\s)", "wikipedia"),
    ("(?<=^|\\s|\\n)OOC(?=[[:punct:]]|$|\\n|\\s)", "out of character"),
    ("yegarra", "yehgarra"),
]
POST_REPLACE = [
    (["Year", "of", "the", "Empire[[:punct:]]*"], "YE", 1),
    (["out", "of", "character[[:punct:]]*"], "OOC", 0),
]

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


@dataclass
class ELVoiceSettings:
    stability: float
    similarity_boost: float


@dataclass
class ELGenerationConfig:
    chunk_length_schedule: list[int]


@dataclass
class ELVoice:
    id: str
    name: str
    use: bool
    model: str
    voice_settings: ELVoiceSettings
    generation_config: ELGenerationConfig


ARTICLE_REPR_KEYS = ["title", "url", "sections"]


def article_repr(article: dict) -> dict:
    return {k: v for k, v in article.items() if k in ARTICLE_REPR_KEYS}


def manuscript_changed(article0: dict, article1: dict) -> bool:
    return article_repr(article0) != article_repr(article1)


def match_target_amplitude(
    sound: pydub.AudioSegment, target_dBFS: float
) -> pydub.AudioSegment:
    change_in_dBFS = target_dBFS - sound.dBFS
    return sound.apply_gain(change_in_dBFS)


async def elevenlabs_tts_alignment(
    text: str, input_voice: dict
) -> tuple[pydub.AudioSegment, list[dict]]:
    voice = ELVoice(**input_voice)
    async with websockets.connect(
        f"wss://api.elevenlabs.io/v1/text-to-speech/{voice.id}/stream-input?model_id={voice.model}"
    ) as websocket:
        body = {
            "text": text,
            "try_trigger_generation": True,
            "xi_api_key": ELEVENLABS_API_KEY,
            "voice_settings": dataclasses.asdict(voice.voice_settings),
            "generation_config": dataclasses.asdict(voice.generation_config),
        }

        await websocket.send(json.dumps(body))
        await websocket.send(json.dumps({"text": ""}))

        audio = b""
        alignment: list[dict] = []
        start = 0
        word: list[tuple[str, int]] = []
        length = 0
        while True:
            r = json.loads(await websocket.recv())
            if r["audio"]:
                audio += base64.b64decode(r["audio"].encode())
            if r["alignment"]:
                for i, (c, a, l) in enumerate(
                    zip(
                        r["alignment"]["chars"],
                        r["alignment"]["charStartTimesMs"],
                        r["alignment"]["charDurationsMs"],
                    )
                ):
                    length += l
                    if word and c.isspace():
                        alignment.append(
                            {
                                "text": "".join(w[0] for w in word),
                                "start": word[0][1],
                                "length": length,
                            }
                        )
                        word = []
                        length = 0
                    else:
                        word.append((c, a + start))

                alignment.append(
                    {
                        "text": "".join(w[0] for w in word),
                        "start": word[0][1],
                        "length": length,
                    }
                )
                word = []
                length = 0
                start += a + l
            if r["isFinal"]:
                break

        return (
            match_target_amplitude(
                pydub.effects.normalize(
                    pydub.AudioSegment.from_mp3(io.BytesIO(audio))  # type: ignore
                ),
                -20.0,
            ),
            alignment,
        )


async def elevenlabs_tts(
    text: str, voice_id: str, model_id: str = "eleven_multilingual_v1"
) -> pydub.AudioSegment:
    try:
        async with httpx.AsyncClient() as client:
            while not (
                r := await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                    json={"model_id": model_id, "text": text},
                    headers={
                        "Accept": "audio/mpeg",
                        "Content-Type": "application/json",
                        "xi-api-key": ELEVENLABS_API_KEY,
                    },
                    timeout=5 * 60,
                )
            ).is_success:
                if r.status_code in [401, 403]:
                    logger.error(f"API key failed, retrying in 10min: {r} - {r.text}")
                    await asyncio.sleep(10 * 60)
                else:
                    logger.warning(f"TTS request not successful: {r}")
                    await asyncio.sleep(10)
    except httpx.ReadTimeout:
        logger.warning(f"One ElevenLabs request timed out, retrying")
    except httpx.RemoteProtocolError:
        logger.warning(f"Server disconnected during one ElevenLabs request, retrying")

    return pydub.AudioSegment.from_mp3(io.BytesIO(r.content))  # type: ignore


def replace_sublist(
    seq: list[dict],
    search: list[str],
    replacement: str,
    offset: int,
    is_regex: bool = False,
) -> list[dict]:
    result = []
    i = 0
    while i < len(seq):
        # if sequence "text" matches search sublist replace with replacement "text"
        # but with first search elements "start" time
        if i <= (len(seq) - offset - len(search)) and all(
            [
                (
                    re.compile(s, re.IGNORECASE).match(d["text"])
                    if is_regex
                    else s.lower() == d["text"].lower()
                )
                for d, s in zip(seq[i + offset : i + offset + len(search)], search)
            ]
        ):
            result.append(
                {
                    "text": (
                        (" ".join(s["text"] for s in seq[i : i + offset]) + " ")
                        if offset
                        else ""
                    )
                    + replacement,
                    "start": seq[i]["start"],
                    "length": sum(
                        s["length"] for s in seq[i : i + offset + len(search)]
                    ),
                }
            )
            i += len(search) + offset
        else:
            result.append(seq[i])
            i += 1

    return result


def generate_voice_from_text(
    text: str, voice: dict
) -> tuple[pydub.AudioSegment, list[dict]]:
    for f0, t0 in GLOBAL_REPLACE:
        text = re.compile(f0, re.IGNORECASE).sub(t0, text)

    texts = [text]
    audio, alignment = asyncio.run(elevenlabs_tts_alignment(text, voice))

    for f1, t1, offset in POST_REPLACE:
        alignment = replace_sublist(alignment, f1, t1, offset)

    return audio, alignment


def text_to_spans(text: str | list[str]) -> list:
    return [
        {"text": t.replace(" ", "").replace("–", "-").strip()}
        for t in (text.split() if isinstance(text, str) else text)
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
        "state": "error",
        "sections": [
            {
                "section_type": "h1",
                "spans": [{"text": article_id.replace("_", " ")}],
            },
            {
                "section_type": "p",
                "spans": [
                    {"text": "This article could not be processed by the system."},
                    {
                        "text": f"This could either be because the article ({article_url}) does not exist or because an error happend while downloading it."
                    },
                    {
                        "text": "The system will regularly retry to process the article in case the problem is only temporary."
                    },
                ],
            },
        ],
    }


def content_to_sections(content: Tag, audio_dir: Path) -> Generator[dict, None, None]:
    for i, child in enumerate(content.findChildren(recursive=False)):
        text = (
            child.text.strip()
            if child.name != "ul" and child.name != "ol"
            else [
                c.text.strip()
                for c in child.findChildren(recursive=False)
                if c.text.strip()
            ]
        )

        if text:
            yield {
                "section_type": child.name,
                "audio_path": str((audio_dir / f"{i+1:04}.mp3").absolute()),
                "audio_url": "/"
                + str((audio_dir / f"{i+1:04}.mp3").relative_to(WEB_DIR)),
                "alignment_path": str((audio_dir / f"{i+1:04}.json").absolute()),
                "alignment_url": "/"
                + str((audio_dir / f"{i+1:04}.json").relative_to(WEB_DIR)),
                "spans": text_to_spans(text),
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
        "state": "generating",
        "sections": [
            {
                "section_type": "h1",
                "audio_path": str((audio_dir / f"{0:04}.mp3").absolute()),
                "audio_url": "/"
                + str((audio_dir / f"{0:04}.mp3").relative_to(WEB_DIR)),
                "alignment_path": str((audio_dir / f"{0:04}.json").absolute()),
                "alignment_url": "/"
                + str((audio_dir / f"{0:04}.json").relative_to(WEB_DIR)),
                "spans": text_to_spans(title),
            },
            *list(content_to_sections(content, audio_dir)),
        ],
        "outro": {
            "audio_path": str((audio_dir / "outro.mp3").absolute()),
            "audio_url": "/" + str((audio_dir / "outro.mp3").relative_to(WEB_DIR)),
        },
    }


def generate_audio(manuscript: dict, task: str) -> None:
    voice = random.choice([v for v in VOICES if v["use"]])

    logger.info(f'Chose voice "{voice["name"]}" for "{manuscript["title"]}"')

    for i, section in enumerate(manuscript["sections"]):
        COLLECTION.update_one(
            {"_id": manuscript["_id"]},
            {"$set": {"progress": i / len(manuscript["sections"])}},
        )

        text = " ".join(s["text"] for s in section["spans"])
        audio, alignment = generate_voice_from_text(text, voice)
        if section["section_type"] == "ul" or section["section_type"] == "ol":
            for s in section["spans"]:
                alignment = replace_sublist(
                    alignment, s["text"].split(), s["text"], 0, False
                )

        audio.export(section["audio_path"])
        json.dump(alignment, open(section["alignment_path"], "w"))
        logger.info(
            f'{i}/{len(manuscript["sections"])-1} TTS audio segments generated for "{manuscript["title"]}"'
        )
    audio, _ = generate_voice_from_text(
        f'This article was read aloud by the artificial voice, "{voice["name"]}". All content of this recording is the original work of Profound Decisions and can be found on the Empire wikipedia. Thank you for listening.',
        voice,
    )
    audio.export(manuscript["outro"]["audio_path"])
    logger.info(f'All TTS audio segments generated for "{manuscript["title"]}"')


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
                if REFRESH_ARTICLES and manuscript_changed(
                    manuscript, existing_manuscript
                ):
                    logger.info(
                        f'Article "{manuscript["title"]}" ({manuscript["url"]}) changed, updating manuscript'
                    )
                    generate_audio(manuscript, "Updating manuscript")
                    manuscript["state"] = "done"
                    insert_or_replace(manuscript)
                if len(manuscript["sections"]) + 1 > len(list(audio_dir.iterdir())):
                    logger.warning(
                        f'Article "{manuscript["title"]}" ({manuscript["url"]}) has fewer generated files ({len(list(audio_dir.iterdir()))}) than needed ({len(manuscript["sections"]) + 1}), regenerating files'
                    )
                    generate_audio(manuscript, "Updating manuscript")
                    manuscript["state"] = "done"
                    insert_or_replace(manuscript)
            else:
                logger.info(
                    f'Article "{manuscript["title"]}" ({manuscript["url"]}) not yet generated, generating manuscript'
                )
                generate_audio(manuscript, "Generating manuscript")
                manuscript["state"] = "done"
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
                "progress": 0.0,
                "title": article_id,
                "url": f"{WIKI_URL}/{article_id}",
                "state": "generating",
                "sections": [
                    {
                        "section_type": "h1",
                        "spans": [{"text": article_id}],
                    },
                    {
                        "section_type": "p",
                        "spans": [
                            {"text": "The system is still processing this article."},
                            {
                                "text": "This will take anywhere from a couple of minutes to hours, depending on the article and how many articles are ahead of this one in the queue."
                            },
                            {
                                "text": "You are welcome to come back to check the progress, but unfortunately the system is not smart enough to give you an estimate."
                            },
                        ],
                    },
                ],
            }
        )
        return {
            "title": article_id,
            "url": f"{WIKI_URL}/{article_id}",
            "state": "generating",
            "sections": [
                {
                    "section_type": "h1",
                    "spans": [{"text": "New Article!"}],
                },
                {
                    "section_type": "p",
                    "spans": [
                        {
                            "text": "Congratulations! You are the first to visit this article!"
                        }
                    ],
                },
                {
                    "section_type": "p",
                    "spans": [
                        {
                            "text": "Unfortunately, this means that the system now have to generate this article."
                        },
                        {
                            "text": "This it will take anywhere from a couple of minutes to hours, depending on the article and how many articles are ahead of this one in the queue."
                        },
                        {
                            "text": "You are welcome to come back later to check again but unfortunately the system is not smart enough to give you an estimate."
                        },
                    ],
                },
            ],
        }


app.mount("/", StaticFiles(directory="/app/web", html=True), name="Web")
