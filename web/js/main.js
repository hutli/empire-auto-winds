let audio_playing = null;
let is_playing = false;
let playback_rate = 1.0;

async function my_play(audios, outro) {
  if (audios.length > 0) {
    document.getElementById(audios[0][0]).classList.add("active_span");
    audios[0][1].addEventListener("ended", () => {
      document.getElementById(audios[0][0]).classList.remove("active_span");
      my_play(audios.slice(1), outro);
    });
    audio_playing = audios[0][1];
  } else {
    audio_playing = outro;
  }

  audio_playing.playbackRate = playback_rate;
  audio_playing.play();
  if (!is_playing) {
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

function do_it(manuscript) {
  let body = document.querySelector("#content");
  body.innerHTML = "";
  let buttons = document.createElement("div");
  buttons.classList.add("article_menu");

  buttons.appendChild(
    createRoundLinkButton(`?year=${p_year}&season=${p_season}`, "&#x21E6")
  );

  let speed_slider_container = document.createElement("fieldset");
  speed_slider_container.classList.add("playback-speed-container");
  let speed_slider_container_legend = document.createElement("legend");
  speed_slider_container_legend.innerText = "Playback speed";
  speed_slider_container.appendChild(speed_slider_container_legend);
  let speed_slider = document.createElement("input");
  speed_slider.type = "range";
  speed_slider.min = "1";
  speed_slider.max = "200";
  speed_slider.value = "100";
  speed_slider.classList.add("max-slider");
  speed_slider.onchange = () => {
    if (audio_playing) {
      playback_rate = speed_slider.value / 100;
      audio_playing.playbackRate = playback_rate;
    }
  };
  speed_slider_container.appendChild(speed_slider);
  buttons.appendChild(speed_slider_container);

  // Resume button
  let resume_btn = createRoundButton("&#x23F5");

  // Pause button
  let pause_btn = createRoundButton("&#x23F8");

  // On click events
  resume_btn.onclick = () => {
    if (audio_playing) {
      is_playing = true;
      resume_btn.classList.add("audio-button-active");
      pause_btn.classList.remove("audio-button-active");
      audio_playing.play();
    }
  };
  pause_btn.onclick = () => {
    if (audio_playing) {
      is_playing = false;
      resume_btn.classList.remove("audio-button-active");
      pause_btn.classList.add("audio-button-active");
      audio_playing.pause();
    }
  };

  buttons.appendChild(resume_btn);
  buttons.appendChild(pause_btn);

  body.appendChild(buttons);
  body.appendChild(document.createElement("hr"));

  let audios = [];
  for (const [i, section] of manuscript.sections.entries()) {
    let elem = document.createElement(section.section_type);
    for (let [ii, span] of section.spans.entries()) {
      let span_elem = document.createElement("span");
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
    body.appendChild(elem);
  }
  audios.forEach(([span_id, _]) => {
    document.getElementById(span_id).onclick = (e) => {
      if (audio_playing) {
        audio_playing.pause();
        audio_playing.currentTime = 0;
        audios.forEach((a, i, _) => {
          document.getElementById(a[0]).classList.remove("active_span");
          if (a[0] == e.target.id) {
            my_play(audios.slice(i), new Audio(manuscript.outro.url));
          }
        });
      }
    };
  });

  body.appendChild(document.createElement("hr"));
  let a = document.createElement("a");
  a.href = manuscript.url;
  a.target = "_blank";
  a.innerText = manuscript.url;
  body.appendChild(a);

  setTimeout(() => {
    resume_btn.classList.add("audio-button-active");
    pause_btn.classList.remove("audio-button-active");
    is_playing = true;
    my_play(audios, new Audio(manuscript.outro.url));
  }, 1000);
}

// MAIN
let params = new URLSearchParams(document.location.search);
let p_year = params.get("year");
let p_season = params.get("season");

let body = document.querySelector("#content");
let buttons = document.createElement("div");
buttons.classList.add("buttons");

console.log(p_year);
console.log(p_season);

if (!p_year) {
  console.log("No year!");

  let years = [];
  fetch("manuscripts.json").then((manuscripts) => {
    manuscripts.json().then((manuscripts) => {
      for (const [_, manuscript] of Object.entries(manuscripts)) {
        if (!years.includes(manuscript.year)) {
          years.push(manuscript.year);
          buttons.appendChild(
            createLinkButton(`?year=${manuscript.year}`, manuscript.year)
          );
        }
      }
    });
  });
} else if (!p_season) {
  console.log("No season!");
  body.appendChild(createLinkButton("/", "Back"));
  body.appendChild(document.createElement("hr"));

  let seasons = [];
  fetch("manuscripts.json").then((manuscripts) => {
    manuscripts.json().then((manuscripts) => {
      for (const [_, manuscript] of Object.entries(manuscripts)) {
        if (manuscript.year == p_year && !seasons.includes(manuscript.season)) {
          seasons.push(manuscript.season);
          buttons.appendChild(
            createLinkButton(
              `?year=${p_year}&season=${manuscript.season}`,
              manuscript.season
            )
          );
        }
      }
    });
  });
} else if (p_year && p_season) {
  console.log("Got year and season, woop!");
  body.appendChild(createLinkButton(`?year=${p_year}`, "Back"));
  body.appendChild(document.createElement("hr"));

  fetch("manuscripts.json").then((manuscripts) => {
    manuscripts.json().then((manuscripts) => {
      for (const [name, manuscript] of Object.entries(manuscripts)) {
        if (manuscript.year == p_year && manuscript.season == p_season) {
          let btn = document.createElement("button");
          btn.textContent = name;
          btn.onclick = () => do_it(manuscript);
          buttons.appendChild(btn);
        }
      }
    });
  });
}

body.appendChild(buttons);
