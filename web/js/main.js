let audio_playing = null;
let is_playing = false;
let playback_rate = 1.0;
let start_delay = 0;

async function my_play(audios, outro) {
  if (audios.length > 0) {
    let elem = document.getElementById(audios[0][0]);
    elem.classList.add("active_span");
    elem.scrollIntoView({
      behavior: "smooth",
      block: "center",
      inline: "center",
    });

    audios[0][1].addEventListener("ended", () => {
      document.getElementById(audios[0][0]).classList.remove("active_span");
      my_play(audios.slice(1), outro);
    });
    audio_playing = audios[0][1];
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
  } else {
    audio_playing.pause();
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
  }
}

function populateManuscriptContent(manuscript) {
  let article_content = document.querySelector("#article-content");

  let audios = [];
  for (const [i, section] of manuscript.sections.entries()) {
    let elem = document.createElement(section.section_type);
    for (let [ii, span] of section.spans.entries()) {
      let span_elem = document.createElement(
        section.section_type != "ul" ? "span" : "li"
      );

      let span_id = `${String(i).padStart(4, "0")}_${String(ii).padStart(
        4,
        "0"
      )}`;
      span_elem.textContent = span.text;
      span_elem.id = span_id;
      span_elem.title = "Start audio from this sentence";
      elem.appendChild(span_elem);
      audios.push([span_id, new Audio(span.audio_url)]);
    }
    elem.innerHTML = elem.innerHTML.replaceAll("</span><", "</span> <");
    article_content.appendChild(elem);
  }

  article_content.appendChild(document.createElement("hr"));
  let a = document.createElement("a");
  a.href = manuscript.url;
  a.target = "_blank";
  a.innerText = manuscript.url;
  article_content.appendChild(a);

  audios.forEach(([span_id, _]) => {
    document.getElementById(span_id).onclick = (e) => {
      if (audio_playing) {
        audio_playing.pause();
        audio_playing.currentTime = 0;
      }
      audios.forEach((a, i) => {
        document.getElementById(a[0]).classList.remove("active_span");
        if (a[0] == e.target.id) {
          my_play(audios.slice(i), new Audio(manuscript.outro.audio_url));
        }
      });
    };
  });

  document.querySelector("#resume-btn").classList.remove("audio-button-active");
  document.querySelector("#pause-btn").classList.add("audio-button-active");
  is_playing = false;
  my_play(audios, new Audio(manuscript.outro.audio_url));
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
