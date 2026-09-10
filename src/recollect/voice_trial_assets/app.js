"use strict";
(() => {
  const data = window.VOICE_TRIAL;
  const notice = document.getElementById("notice");
  if (!data || !Array.isArray(data.cases) || !data.cases.length) {
    notice.textContent = "The recordings could not load. Please refresh this page.";
    return;
  }
  const punctuation = data.mode === "punctuation";
  const key = `recollect-${punctuation ? "punctuation" : "dual"}-${data.source_sha256}`;
  let feedback = {version: 1, source_sha256: data.source_sha256, responses: {}};
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (saved && saved.source_sha256 === data.source_sha256 && saved.responses) feedback = saved;
  } catch { notice.textContent = "Device storage is unavailable. Download feedback before leaving."; }
  let selected = 0;
  let variation = 0;
  function current() {
    const id = data.cases[selected].id;
    return feedback.responses[id] ||= {preference: "", notes: "", played: [], ended: []};
  }
  function save() {
    feedback.updated_at = new Date().toISOString();
    try { localStorage.setItem(key, JSON.stringify(feedback)); }
    catch { notice.textContent = "Device storage is unavailable. Download feedback before leaving."; }
  }
  function element(tag, text, className) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (className) el.className = className;
    return el;
  }
  function stopAudio() {
    document.querySelectorAll("audio").forEach(audio => audio.pause());
  }
  function recordAudio(clip, field) {
    const entry = current();
    const id = `${clip.arm}-${clip.repeat}`;
    if (!entry[field].includes(id)) entry[field].push(id);
    save();
  }
  function card(clip, title, variantPicker) {
    const box = element("article", undefined, "card");
    box.append(element("h3", title));
    if (variantPicker) {
      const label = element("label", "Compare variation");
      label.htmlFor = "variation";
      const select = element("select");
      select.id = "variation";
      ["First response", "Another response · same prompt"].forEach((text, index) => {
        const option = element("option", text); option.value = index; select.append(option);
      });
      select.value = variation;
      select.addEventListener("change", () => { variation = Number(select.value); renderPlayers(); });
      box.append(label, select);
    }
    const audio = element("audio");
    audio.controls = true; audio.preload = "metadata"; audio.src = clip.file;
    audio.setAttribute("aria-label", `${title}, ${data.cases[selected].title}`);
    audio.addEventListener("play", () => {
      document.querySelectorAll("audio").forEach(other => { if (other !== audio) other.pause(); });
      recordAudio(clip, "played");
    });
    audio.addEventListener("ended", () => recordAudio(clip, "ended"));
    audio.addEventListener("error", () => { notice.textContent = "A recording could not load. Refresh the page or try again later."; });
    const seconds = Math.round(clip.audio.duration_s);
    box.append(audio, element("div", `${seconds} seconds · synthetic Kokoro audio`, "meta"));
    box.append(element("p", punctuation ? "Qwen's response" : "Displayed response", "meta"));
    box.append(element("p", clip.display, "display-text"));
    const details = element("details");
    details.append(element("summary", punctuation ? "Text sent to Kokoro" : "Spoken text and Qwen output"));
    details.append(element("p", clip.speech));
    if (!punctuation) details.append(element("pre", clip.raw));
    box.append(details);
    return box;
  }
  function renderPlayers() {
    stopAudio();
    const clips = data.cases[selected].clips;
    document.getElementById("players").replaceChildren(
      card(clips[0], punctuation ? "Original prompt" : "Current prompt", false),
      card(clips[variation + 1], punctuation ? "Conversational prompt" : "Display + speech prompt", !punctuation)
    );
  }
  function render() {
    const item = data.cases[selected];
    document.getElementById("case-title").textContent = item.title;
    document.getElementById("question").textContent = item.question;
    document.getElementById("memory").textContent = item.memory;
    document.querySelectorAll("#cases button").forEach((button, index) => {
      button.setAttribute("aria-current", String(index === selected));
    });
    renderPlayers();
    const choices = document.getElementById("choices");
    choices.replaceChildren();
    const preferences = punctuation ? ["Original", "Conversational prompt", "Similar", "Neither"]
      : ["Current prompt", "Dual first", "Dual another", "Similar", "Neither"];
    preferences.forEach(value => {
      const label = element("label");
      const radio = element("input");
      radio.type = "radio"; radio.name = "preference"; radio.value = value;
      radio.checked = current().preference === value;
      radio.addEventListener("change", () => { current().preference = value; save(); });
      label.append(radio, document.createTextNode(value)); choices.append(label);
    });
    document.getElementById("notes").value = current().notes;
  }
  data.cases.forEach((item, index) => {
    const button = element("button", `${index + 1}. ${item.title}`);
    button.addEventListener("click", () => { selected = index; variation = 0; render(); });
    document.getElementById("cases").append(button);
  });
  document.getElementById("notes").addEventListener("input", event => {
    current().notes = event.target.value; save();
  });
  document.getElementById("export").addEventListener("click", () => {
    save();
    const blob = new Blob([JSON.stringify(feedback, null, 2)], {type: "application/json"});
    const url = URL.createObjectURL(blob);
    const link = element("a"); link.href = url;
    link.download = `recollect-${punctuation ? "punctuation" : "qwen-kokoro"}-${Date.now()}.json`;
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    notice.textContent = "Feedback downloaded. You can send that file back here.";
  });
  render();
})();
