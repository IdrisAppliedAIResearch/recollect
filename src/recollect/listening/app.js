/* The kit is static: no microphone, conversation API, or external requests. */
(() => {
  'use strict';
  const metrics = ['Naturalness', 'Clarity', 'Comfort'];
  function shuffled(items, random) {
    const result = [...items];
    for (let i = result.length - 1; i > 0; i--) {
      const j = Math.floor(random() * (i + 1));
      [result[i], result[j]] = [result[j], result[i]];
    }
    return result;
  }
  function newSession(kit, random = Math.random) {
    const shorter = kit.comparisons.filter(pair => pair.stage !== 'Long listen');
    const longer = kit.comparisons.filter(pair => pair.stage === 'Long listen');
    return {
      id: globalThis.crypto.randomUUID(), created_at: new Date().toISOString(),
      kit_id: kit.id, environment: '', index: 0,
      trials: [...shuffled(shorter, random), ...longer].map(pair => ({
        pair_id: pair.id, order: shuffled(pair.profiles, random),
        ratings: { A: {}, B: {} }, listened: { A: 0, B: 0 },
        play_events: [], preference: '', notes: '', revealed_at: null, saved_at: null,
      })),
    };
  }
  function complete(trial) {
    return ['A', 'B'].every(side => trial.listened[side] > 0 &&
      metrics.every(metric => Number.isInteger(trial.ratings[side][metric]) &&
        trial.ratings[side][metric] >= 1 && trial.ratings[side][metric] <= 5)) &&
      ['A', 'B', 'tie', 'neither'].includes(trial.preference);
  }
  function exportData(kit, sessions) {
    return { schema: 1, exported_at: new Date().toISOString(), kit, sessions };
  }
  if (typeof module !== 'undefined') module.exports = { newSession, complete, exportData };
  if (typeof document === 'undefined') return;

  const kit = window.LISTENING_KIT;
  const $ = id => document.getElementById(id);
  if (!kit) { $('notice').textContent = 'The audio manifest is missing. Regenerate this kit.'; return; }
  if (kit.round === 2) {
    $('round-label').textContent = 'ROUND TWO · ROOM FOR A THOUGHT';
    const minutes = kit.comparisons.reduce((sum, pair) => sum + pair.profiles.reduce(
      (duration, profile) => duration + kit.clips[`${pair.sample}--${profile}`].duration_s, 0), 0) / 60;
    $('intro-copy').textContent = `Your feedback, a new set of deliveries. Eight comparisons, about ${Math.ceil(minutes)} minutes of audio. Listen for sharp S sounds, space between thoughts, and whether the rhythm stays comfortable. Ties are useful; take breaks freely.`;
    $('method-copy').textContent = 'The pause-variance pairs reuse identical speech audio and the same total added silence, redistributed according to the length of the preceding thought. Clause, sentence, and paragraph breaks have separate budgets. Boundary control tests whether splitting at these points changes delivery by itself. Pace variance is tested separately, using small native synthesis-rate changes around 1.025×. The final long pair combines changes, so it cannot identify which one caused a preference. These assembled recordings exclude live playback stalls and have no loudness normalization. Timing is repeatable, with no random jitter. Original ratings remain with round one.';
  }
  const storageKey = `recollect-listening-v1:${kit.id}`;
  let sessions = [], active = 0, players = [], discarded = null;
  const notice = message => { $('notice').textContent = message; };
  try {
    const stored = JSON.parse(localStorage.getItem(storageKey) || 'null');
    if (stored) {
      if (!Array.isArray(stored.sessions) || !stored.sessions.length ||
          !stored.sessions.every(s => s.kit_id === kit.id && Array.isArray(s.trials) &&
            s.trials.length === kit.comparisons.length)) throw new Error('Invalid saved sessions');
      sessions = stored.sessions;
      active = Math.max(0, Math.min(stored.active || 0, sessions.length - 1));
    }
  } catch {
    notice('Saved progress could not be loaded. This session still works; export your ratings before closing.');
  }
  if (!sessions.length) sessions = [newSession(kit)];
  const session = () => sessions[active];
  const trial = () => session().trials[session().index];
  function persist() {
    try { localStorage.setItem(storageKey, JSON.stringify({ sessions, active })); }
    catch {
      $('storage-note').textContent = 'Browser storage is unavailable. Export ratings before closing this page.';
    }
  }
  function stopAudio() { players.forEach(audio => audio.pause()); }
  function updateProgress() {
    const count = session().trials.filter(t => t.saved_at && complete(t)).length;
    const total = session().trials.length;
    $('progress').max = total;
    $('progress').value = count;
    $('progress-label').textContent = `${count} of ${total} saved`;
    $('progress-percent').textContent = `${Math.round(count / total * 100)}%`;
    for (const [index, button] of [...$('trials').children].entries()) {
      const item = session().trials[index];
      button.lastChild.textContent = item.saved_at && complete(item) ? '✓' : '';
    }
  }
  function options(select, choices, selected = '') {
    select.replaceChildren(...choices.map(([value, label]) => {
      const option = document.createElement('option');
      option.value = value; option.textContent = label;
      return option;
    }));
    select.value = selected;
  }
  function reveal() {
    const t = trial();
    if (!t.revealed_at) t.revealed_at = new Date().toISOString();
    const pair = kit.comparisons.find(p => p.id === t.pair_id);
    $('identities').hidden = false;
    $('identities').textContent = ['A', 'B'].map((side, index) => {
      const profile = kit.profiles[t.order[index]];
      const clip = kit.clips[`${pair.sample}--${profile.id}`];
      return `${side}: ${profile.label}\nVoice: ${profile.voice}` +
        `${profile.blend_voice ? ` + ${profile.blend_weight * 100}% ${profile.blend_voice}` : ''}` +
        `\nSpeed: ${profile.speed}× · Grouping: ${profile.grouping}` +
        (profile.pause_policy
          ? `\nPause policy: ${profile.pause_policy} · Pace: ${profile.pace_policy}` +
            `\n${(clip.added_silence_frames / clip.sample_rate || 0).toFixed(3)}s added silence` +
            (clip.boundaries ? `\nAdded pauses (ms): ${clip.boundaries.map(b => Math.round(b.added_pause_s * 1000)).join(', ')}` +
              `\nPhrase speeds: ${clip.boundaries.map(b => b.speed.toFixed(3)).join(', ')}` : '')
          : `\nExtra sentence-boundary pause: ${profile.boundary_pause_s}s`) +
        `\n${clip.pieces.length} chunks · ${clip.duration_s.toFixed(1)}s audio` +
        `\nFirst chunk synthesis: ${clip.first_chunk_synthesis_s.toFixed(3)}s (offline)`;
    }).join('\n\n');
    persist();
  }
  function render() {
    stopAudio(); players = [];
    const s = session(), t = trial();
    const pair = kit.comparisons.find(p => p.id === t.pair_id);
    const sample = kit.samples[pair.sample];
    options($('session'), sessions.map((item, i) => [String(i),
      `Session ${i + 1} · ${new Date(item.created_at).toLocaleDateString()}`]), String(active));
    $('environment').value = s.environment;
    $('stage').textContent = pair.stage;
    $('position').textContent = `${s.index + 1} / ${s.trials.length}`;
    $('sample-title').textContent = sample.title;
    $('trial-help').textContent = pair.stage === 'Long listen'
      ? 'Settle in for a few minutes. Notice effort or fatigue as each recording continues. Take a break between clips if needed.'
      : 'Play both recordings to the end. Rate each one, then choose your preference.';
    $('sample-text').textContent = sample.text;
    $('trials').replaceChildren(...s.trials.map((item, index) => {
      const p = kit.comparisons.find(row => row.id === item.pair_id);
      const button = document.createElement('button');
      const check = document.createElement('span'); check.className = 'check';
      button.append(`${String(index + 1).padStart(2, '0')}  ${p.stage}`, check);
      button.setAttribute('aria-label', `${index + 1}: ${p.stage}, ${kit.samples[p.sample].title}`);
      if (index === s.index) button.setAttribute('aria-current', 'step');
      button.onclick = () => { s.index = index; persist(); notice(''); render(); };
      return button;
    }));
    $('players').replaceChildren();
    for (const [index, side] of ['A', 'B'].entries()) {
      const card = $('player-template').content.firstElementChild.cloneNode(true);
      card.querySelector('.letter').textContent = side;
      const heard = card.querySelector('.heard');
      heard.textContent = t.listened[side] ? `Played to end ×${t.listened[side]}` : 'Not played yet';
      const audio = card.querySelector('audio');
      audio.setAttribute('aria-label', `Recording ${side}`);
      audio.src = kit.clips[`${pair.sample}--${t.order[index]}`].file;
      audio.onplay = () => {
        players.filter(p => p !== audio).forEach(p => p.pause());
        t.play_events.push({ side, type: 'play', at: new Date().toISOString(), position_s: audio.currentTime });
        persist();
      };
      audio.onended = () => {
        t.listened[side] += 1;
        t.play_events.push({ side, type: 'ended', at: new Date().toISOString() });
        heard.textContent = `Played to end ×${t.listened[side]}`;
        persist();
      };
      audio.onerror = () => notice(`Recording ${side} could not load. Keep the audio folder with this page.`);
      players.push(audio);
      for (const metric of metrics) {
        const row = document.createElement('div'); row.className = 'rating';
        const label = document.createElement('label');
        const select = document.createElement('select');
        select.id = `rating-${side}-${metric}`; label.htmlFor = select.id; label.textContent = metric;
        options(select, [['', 'Choose…'], ['1', '1 · Very low'], ['2', '2 · Low'],
          ['3', '3 · Moderate'], ['4', '4 · High'], ['5', '5 · Very high']],
        String(t.ratings[side][metric] || ''));
        select.onchange = () => {
          if (select.value) t.ratings[side][metric] = Number(select.value);
          else delete t.ratings[side][metric];
          t.saved_at = null; persist(); updateProgress();
        };
        row.append(label, select); card.querySelector('.ratings').append(row);
      }
      const note = document.createElement('div'); note.className = 'scale-note';
      note.textContent = 'Naturalness: human-like delivery. Clarity: easy to understand. Comfort: pleasant, without effort or fatigue.';
      card.append(note); $('players').append(card);
    }
    $('preferences').replaceChildren(...[['A', 'Prefer A'], ['B', 'Prefer B'],
      ['tie', 'About the same'], ['neither', 'Neither feels right']].map(([value, text]) => {
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'radio'; input.name = 'preference'; input.value = value;
      input.checked = t.preference === value;
      input.onchange = () => { t.preference = value; t.saved_at = null; persist(); updateProgress(); };
      label.append(input, text); return label;
    }));
    $('notes').value = t.notes;
    $('previous').disabled = s.index === 0;
    $('save').textContent = s.index === s.trials.length - 1 ? 'Save comparison ✓' : 'Save & next →';
    $('identities').hidden = true;
    if (t.revealed_at) reveal();
    updateProgress();
  }
  $('notes').oninput = () => { trial().notes = $('notes').value; persist(); };
  $('environment').oninput = () => { session().environment = $('environment').value; persist(); };
  $('previous').onclick = () => { session().index -= 1; persist(); notice(''); render(); };
  $('session').onchange = () => { active = Number($('session').value); persist(); notice(''); render(); };
  $('new-session').onclick = () => {
    sessions.push(newSession(kit)); active = sessions.length - 1; persist();
    notice('New session started. Earlier ratings are kept, and A/B order is shuffled again.'); render();
  };
  $('discard').onclick = () => {
    stopAudio();
    discarded = sessions.splice(active, 1)[0];
    if (!sessions.length) sessions.push(newSession(kit));
    active = Math.min(active, sessions.length - 1);
    $('undo-discard').hidden = false;
    persist(); render();
    notice('Session discarded. Undo is available until another discard or a page reload.');
  };
  $('undo-discard').onclick = () => {
    if (!discarded) return;
    sessions.push(discarded); discarded = null; active = sessions.length - 1;
    $('undo-discard').hidden = true;
    persist(); render(); notice('Discarded session restored.');
  };
  $('reveal').onclick = reveal;
  $('save').onclick = () => {
    if (!complete(trial())) {
      notice('Play both recordings to the end, choose all six ratings, and select a preference before saving.');
      return;
    }
    trial().saved_at = new Date().toISOString();
    const finished = session().trials.every(t => t.saved_at && complete(t));
    if (session().index < session().trials.length - 1) session().index += 1;
    persist(); render();
    notice(finished ? 'Session complete. Export your ratings, then return another day for a fresh comparison.'
      : 'Comparison saved. Take a break whenever you need one.');
  };
  $('export').onclick = () => {
    const blob = new Blob([JSON.stringify(exportData(kit, sessions), null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a'); link.href = url;
    link.download = `recollect-listening-${kit.id.slice(0, 8)}-${Date.now()}.json`;
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    notice('Ratings exported with the exact audio settings and A/B assignments.');
  };
  window.addEventListener('pagehide', () => { stopAudio(); persist(); });
  render(); persist();
})();
