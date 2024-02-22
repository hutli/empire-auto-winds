let audio_playing = null;
let is_playing = false;
let playback_rate = 1.0;
let start_delay = 0;
let TIMEOUTS = [];
let CURRENT_AUDIOS = [];

function my_highlight(span, length) {
  if (is_playing) {
    let elem = document.getElementById(span);
    elem.classList.add("active_span");
    elem.scrollIntoView({
      behavior: "smooth",
      block: "center",
      inline: "center",
    });

    setTimeout(() => {
      document.getElementById(span).classList.remove("active_span");
    }, length);
  }
}

function start_highlights(audios) {
  for (let i = 0; i < audios[0][0].length; i++) {
    let timeout =
      (audios[0][1][i].start - audio_playing.currentTime * 1000) /
      playback_rate;
    if (timeout >= 0) {
      TIMEOUTS.push(
        setTimeout(() => {
          my_highlight(audios[0][0][i], audios[0][1][i].length / playback_rate);
        }, timeout),
      );
    }
  }
}

function clear_highlights() {
  for (let h of TIMEOUTS) {
    clearTimeout(h);
  }
  TIMEOUTS = [];
}

async function my_play(audios, outro) {
  CURRENT_AUDIOS = audios;
  if (audios.length > 0) {
    audios[0][2].addEventListener("ended", () => {
      my_play(audios.slice(1), outro);
    });
    audio_playing = audios[0][2];
  } else {
    audio_playing = outro;
    audio_playing.addEventListener("ended", () => {
      pausePlayback();
      audio_playing = null;
    });
  }

  audio_playing.playbackRate = playback_rate;
  if (is_playing) {
    audio_playing.play();
    start_highlights(audios);
  } else {
    audio_playing.pause();
    clear_highlights(audios);
  }
}

function changeFontSize(e) {
  document.querySelector("body").style.fontSize = e.value;
}

function changeFont(e) {
  if (e.value == "Arial") {
    document.querySelector("body").classList.add("arial");
  } else if (e.value == "Open Dyslexic") {
    document.querySelector("body").classList.remove("arial");
  } else {
    console.error(`Unknown font ${e.value}`);
  }
}

function changeLineHeight(e) {
  document
    .querySelectorAll("p")
    .forEach((ee) => (ee.style.lineHeight = e.value));
}

function createLinkButton(href, button_text) {
  let a = document.createElement("a");
  a.href = href;
  let btn = document.createElement("button");
  btn.innerText = button_text;
  a.appendChild(btn);
  return a;
}

function createRoundButton(innerHTML) {
  let btn_inner = document.createElement("b");
  btn_inner.innerHTML = innerHTML;
  btn_inner.classList.add("fas");
  let btn = document.createElement("div");
  btn.classList.add("audio-button");
  btn.appendChild(btn_inner);
  return btn;
}

function createRoundLinkButton(href, innerHTML) {
  let a = document.createElement("a");
  a.style.textDecoration = "none";
  a.style.margin = "auto";
  a.href = href;
  a.appendChild(createRoundButton(innerHTML));
  return a;
}

function changePlaybackSpeed() {
  if (audio_playing) {
    slider = document.querySelector("#playback-speed-slider");
    playback_rate = slider.value / 100;
    audio_playing.playbackRate = playback_rate;
    clear_highlights(CURRENT_AUDIOS);
    if (is_playing) {
      start_highlights(CURRENT_AUDIOS);
    }
  }
}

function resumePlayback() {
  if (audio_playing) {
    is_playing = true;
    document.querySelector("#resume-btn").classList.add("audio-button-active");
    document
      .querySelector("#pause-btn")
      .classList.remove("audio-button-active");
    audio_playing.play();
    start_highlights(CURRENT_AUDIOS);
  }
}

function pausePlayback() {
  if (audio_playing) {
    is_playing = false;
    document
      .querySelector("#resume-btn")
      .classList.remove("audio-button-active");
    document.querySelector("#pause-btn").classList.add("audio-button-active");
    audio_playing.pause();
    clear_highlights(CURRENT_AUDIOS);
  }
}

async function populateManuscriptContent(manuscript) {
  let article_content = document.querySelector("#article-content");
  let progress = document.createElement("p");

  if (manuscript.state == "generating") {
    if (manuscript.progress == 0) {
      progress.innerText = `Waiting - Article still in queue...`;
      article_content.appendChild(progress);
    } else {
      progress.innerText = `Generating article - ${(
        manuscript.progress * 100
      ).toFixed(2)}%`;
      article_content.appendChild(progress);
    }
  } else if (manuscript.state == "done" && manuscript.state == "error") {
    if (manuscript.progress > 0 && manuscript.progress < 100) {
      progress.innerText = `Updating article - ${(
        manuscript.progress * 100
      ).toFixed(2)}%`;
      article_content.appendChild(progress);
    }
  }

  let audios = [];
  for (const [i, section] of manuscript.sections.entries()) {
    let section_elem = document.createElement(section.section_type);
    section_elem.classList.add("section");
    section_elem.id = i;
    section_elem.title = "Start audio from this section";
    section_elem.onclick = () => {
      if (audio_playing) {
        audio_playing.pause();
        audio_playing.currentTime = 0;
      }

      clear_highlights();
      my_play(audios.slice(i), new Audio(manuscript.outro.audio_url));
    };

    let span_ids = [];
    for (let [ii, span] of section.spans.entries()) {
      let span_elem = document.createElement(
        section.section_type != "ul" && section.section_type != "ol"
          ? "span"
          : "li",
      );

      let span_id = `${String(i).padStart(4, "0")}_${String(ii).padStart(
        4,
        "0",
      )}`;
      span_ids.push(span_id);
      span_elem.textContent = span.text;
      span_elem.id = span_id;
      section_elem.appendChild(span_elem);
    }
    if (section.alignment_url && section.audio_url) {
      audios.push([
        span_ids,
        await (await fetch(section.alignment_url)).json(),
        new Audio(section.audio_url),
      ]);
    }
    section_elem.innerHTML = section_elem.innerHTML.replaceAll(
      "</span><",
      "</span> <",
    );
    article_content.appendChild(section_elem);
  }

  article_content.appendChild(document.createElement("hr"));
  let a = document.createElement("a");
  a.href = manuscript.url;
  a.target = "_blank";
  a.innerText = manuscript.url;
  article_content.appendChild(a);

  document.querySelector("#resume-btn").classList.remove("audio-button-active");
  document.querySelector("#pause-btn").classList.add("audio-button-active");
  is_playing = false;
  if (manuscript.outro && manuscript.outro.audio_url) {
    my_play(audios, new Audio(manuscript.outro.audio_url));
  }
}

function alphabeticallyRankName(a, b) {
  if (a.name < b.name) {
    return -1;
  }
  if (a.name > b.name) {
    return 1;
  }
  return 0;
}

// MAIN
let params = new URLSearchParams(document.location.search);
let p_name = params.get("name");
if (!p_name) {
  p_name = location.pathname.split("/").slice(-1)[0];
}

if (p_name) {
  fetch(`/api/manuscript/${p_name}`).then((response) => {
    if (response.status == 200) {
      response.json().then((manuscript) => {
        populateManuscriptContent(manuscript);
      });
    }
  });
}
