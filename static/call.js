const state = {
  ws: null,
  mediaRecorder: null,
  audioChunks: [],
  stream: null,
  remainingS: 300,
  playbackTimer: null,
  recordingTimer: null,
};

const audioQueue = [];
let isPlayingAudio = false;

const el = (id) => document.getElementById(id);

async function loadScenarios() {
  const res = await fetch('/api/scenarios');
  const scenarios = await res.json();
  const list = el('scenario-list');
  list.innerHTML = '';
  scenarios.forEach((s) => {
    const card = document.createElement('div');
    card.className = 'scenario-card';
    card.innerHTML = `
      <div class="info">
        <h2>${s.area} &mdash; ${s.scenario_id}</h2>
        <p>${s.process_name} &middot; Difficulty: ${s.difficulty} &middot; ${s.duration_minutes} min</p>
      </div>
      <button>Start Call</button>
    `;
    card.querySelector('button').addEventListener('click', () => startCall(s));
    list.appendChild(card);
  });
}

function showView(id) {
  document.querySelectorAll('.view').forEach((v) => { v.hidden = true; });
  el(id).hidden = false;
}

function formatTime(totalSeconds) {
  const m = Math.floor(totalSeconds / 60);
  const s = Math.floor(totalSeconds % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

// The 5-minute budget only counts actual speaking time — the customer's
// audio playing, or the trainee actively recording. Transcription/thinking/
// synthesis time in between is free and does not tick the timer, so it
// never eats the whole budget on processing latency alone.

function setBaseRemaining(seconds) {
  state.remainingS = Math.max(0, seconds);
  updateTimerDisplay();
}

function updateTimerDisplay() {
  const timerEl = el('call-timer');
  timerEl.textContent = formatTime(state.remainingS);
  timerEl.classList.toggle('warning', state.remainingS <= 30);
}

function startPlaybackTicking(audioEl) {
  clearInterval(state.playbackTimer);
  const base = state.remainingS;
  state.playbackTimer = setInterval(() => {
    state.remainingS = Math.max(0, base - (audioEl.currentTime || 0));
    updateTimerDisplay();
  }, 200);
}

function stopPlaybackTicking() {
  clearInterval(state.playbackTimer);
  state.playbackTimer = null;
}

function startRecordingTicking() {
  clearInterval(state.recordingTimer);
  const base = state.remainingS;
  const startedAt = performance.now();
  state.recordingTimer = setInterval(() => {
    const elapsed = (performance.now() - startedAt) / 1000;
    state.remainingS = Math.max(0, base - elapsed);
    updateTimerDisplay();
  }, 200);
}

function stopRecordingTicking() {
  clearInterval(state.recordingTimer);
  state.recordingTimer = null;
}

function stopAllTicking() {
  stopPlaybackTicking();
  stopRecordingTicking();
}

function addTranscriptLine(speaker, text) {
  const wrap = el('transcript');
  const div = document.createElement('div');
  div.className = `line ${speaker}`;
  const label = speaker === 'customer' ? 'Customer' : 'You';
  div.innerHTML = `<span class="speaker">${label}</span>${escapeHtml(text)}`;
  wrap.appendChild(div);
  wrap.scrollTop = wrap.scrollHeight;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function setStatus(text) {
  el('call-status').textContent = text;
}

function setTalkEnabled(enabled) {
  el('talk-btn').disabled = !enabled;
  const skipBtn = el('time-skip-btn');
  if (!skipBtn.hidden) {
    skipBtn.disabled = !enabled;
  }
}

async function startCall(scenario) {
  showView('view-call');
  el('call-scenario-name').textContent = `${scenario.area} — ${scenario.scenario_id}`;
  el('call-scenario-meta').textContent = `${scenario.process_name} · Difficulty: ${scenario.difficulty}`;
  el('transcript').innerHTML = '';
  setStatus('Connecting…');
  setTalkEnabled(false);

  const skipBtn = el('time-skip-btn');
  if (scenario.time_skip_label) {
    skipBtn.textContent = scenario.time_skip_label;
    skipBtn.hidden = false;
    skipBtn.disabled = true;
  } else {
    skipBtn.hidden = true;
  }

  try {
    state.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    setStatus('Microphone access denied — allow microphone access in your browser and try again.');
    return;
  }

  setBaseRemaining(scenario.duration_minutes * 60);

  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  state.ws = new WebSocket(`${protocol}://${location.host}/ws/call/${scenario.scenario_id}`);
  state.ws.binaryType = 'arraybuffer';

  state.ws.addEventListener('open', () => setStatus('Waiting for customer…'));
  state.ws.addEventListener('message', handleWsMessage);
  state.ws.addEventListener('close', stopAllTicking);
  state.ws.addEventListener('error', () => setStatus('Connection error.'));
}

function handleWsMessage(event) {
  if (event.data instanceof ArrayBuffer) {
    queueAudio(event.data);
    return;
  }
  const msg = JSON.parse(event.data);
  switch (msg.type) {
    case 'customer_line':
      addTranscriptLine('customer', msg.text);
      if (typeof msg.remaining_s === 'number') {
        setBaseRemaining(msg.remaining_s);
      }
      setStatus('Customer is speaking…');
      setTalkEnabled(false);
      break;
    case 'trainee_turn_transcribed':
      addTranscriptLine('trainee', msg.text);
      setStatus('Processing…');
      break;
    case 'call_ended':
      onCallEnded();
      break;
    case 'evaluating':
      setStatus('Evaluating your call…');
      break;
    case 'evaluation_result':
      showEvaluation(msg.result);
      break;
    case 'error':
      setStatus(`Error: ${msg.message}`);
      break;
    default:
      break;
  }
}

function queueAudio(buffer) {
  audioQueue.push(buffer);
  playNextInQueue();
}

function playNextInQueue() {
  if (isPlayingAudio || audioQueue.length === 0) return;
  isPlayingAudio = true;
  const buffer = audioQueue.shift();
  const blob = new Blob([buffer], { type: 'audio/wav' });
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  audio.addEventListener('play', () => startPlaybackTicking(audio));
  audio.addEventListener('ended', () => {
    stopPlaybackTicking();
    URL.revokeObjectURL(url);
    isPlayingAudio = false;
    if (audioQueue.length > 0) {
      playNextInQueue();
    } else {
      onCustomerFinishedSpeaking();
    }
  });
  audio.play().catch(() => {
    stopPlaybackTicking();
    isPlayingAudio = false;
    onCustomerFinishedSpeaking();
  });
}

function onCustomerFinishedSpeaking() {
  if (el('view-call').hidden) return;
  setStatus('Your turn — hold the button and speak.');
  setTalkEnabled(true);
}

function beginRecording() {
  if (!state.stream || el('talk-btn').disabled) return;
  state.audioChunks = [];
  const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
    ? 'audio/webm;codecs=opus'
    : '';
  state.mediaRecorder = mimeType
    ? new MediaRecorder(state.stream, { mimeType })
    : new MediaRecorder(state.stream);
  state.mediaRecorder.ondataavailable = (e) => {
    if (e.data.size > 0) state.audioChunks.push(e.data);
  };
  state.mediaRecorder.onstop = onRecordingStop;
  state.mediaRecorder.start();
  el('talk-btn').classList.add('recording');
  el('talk-btn-label').textContent = 'Release to Send';
  setStatus('Listening…');
  startRecordingTicking();
}

function endRecording() {
  if (state.mediaRecorder && state.mediaRecorder.state === 'recording') {
    state.mediaRecorder.stop();
  }
  el('talk-btn').classList.remove('recording');
  el('talk-btn-label').textContent = 'Hold to Talk';
  stopRecordingTicking();
}

async function onRecordingStop() {
  const blob = new Blob(state.audioChunks, { type: state.mediaRecorder.mimeType || 'audio/webm' });
  if (blob.size < 500) {
    setStatus('Your turn — hold the button and speak.');
    return;
  }
  setTalkEnabled(false);
  setStatus('Sending…');
  const buffer = await blob.arrayBuffer();
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(buffer);
  }
}

function endCall() {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: 'end_call' }));
  }
  setTalkEnabled(false);
}

function requestTimeSkip() {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: 'advance_time' }));
  }
  setTalkEnabled(false);
  setStatus('Time is passing…');
}

function onCallEnded() {
  stopAllTicking();
  if (state.stream) {
    state.stream.getTracks().forEach((t) => t.stop());
  }
  setTalkEnabled(false);
  setStatus('Call ended — evaluating…');
}

function showEvaluation(result) {
  showView('view-eval');

  el('eval-score').textContent = `${result.overall_score} / 100`;
  el('eval-stars').textContent = '★'.repeat(result.csat_stars) + '☆'.repeat(5 - result.csat_stars);
  el('eval-band').textContent = result.band;
  el('eval-recommendation').textContent = result.recommendation;

  const categories = [
    ['Process & KB', result.category_scores.process_kb, 45],
    ['Communication', result.category_scores.communication, 20],
    ['Fluency', result.category_scores.fluency, 15],
    ['Pronunciation', result.category_scores.pronunciation, 15],
    ['Call Management', result.category_scores.call_management, 5],
  ];
  const catsEl = el('eval-categories');
  catsEl.innerHTML = '';
  categories.forEach(([label, value, max]) => {
    const row = document.createElement('div');
    row.className = 'eval-cat-row';
    const pct = Math.round((value / max) * 100);
    row.innerHTML = `
      <div class="label">${label}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
      <div class="value">${value}/${max}</div>
    `;
    catsEl.appendChild(row);
  });

  fillList('eval-strengths', result.findings.strengths);
  fillList('eval-improvements', result.findings.improvements);

  const errorsEl = el('eval-errors');
  errorsEl.innerHTML = '';
  const allErrors = [
    ...(result.findings.critical_errors || []).map((e) => ({ ...e, sevLabel: 'critical' })),
    ...(result.findings.major_errors || []).map((e) => ({ ...e, sevLabel: 'major' })),
  ];
  if (allErrors.length === 0) {
    errorsEl.innerHTML = '<p>No critical or major process violations detected.</p>';
  } else {
    allErrors.forEach((e) => {
      const div = document.createElement('div');
      div.className = 'err-item';
      div.innerHTML = `<span class="sev ${e.sevLabel}">${e.sevLabel}</span>${escapeHtml(e.description)}`;
      errorsEl.appendChild(div);
    });
  }
}

function fillList(elementId, items) {
  const listEl = el(elementId);
  listEl.innerHTML = '';
  (items || []).forEach((text) => {
    const li = document.createElement('li');
    li.textContent = text;
    listEl.appendChild(li);
  });
}

function resetToPicker() {
  showView('view-picker');
  if (state.ws) {
    try { state.ws.close(); } catch (err) { /* already closed */ }
  }
  stopAllTicking();
}

el('talk-btn').addEventListener('mousedown', beginRecording);
el('talk-btn').addEventListener('mouseup', endRecording);
el('talk-btn').addEventListener('mouseleave', () => {
  if (state.mediaRecorder && state.mediaRecorder.state === 'recording') endRecording();
});
el('talk-btn').addEventListener('touchstart', (e) => { e.preventDefault(); beginRecording(); });
el('talk-btn').addEventListener('touchend', (e) => { e.preventDefault(); endRecording(); });

el('time-skip-btn').addEventListener('click', requestTimeSkip);
el('end-call-btn').addEventListener('click', endCall);
el('retry-btn').addEventListener('click', resetToPicker);

loadScenarios();
