const state = {
  ws: null,
  mediaRecorder: null,
  audioChunks: [],
  stream: null,
  remainingS: 300,
  playbackTimer: null,
  recordingTimer: null,
  isRecording: false,
  currentScenario: null,
  transcriptLog: [],
};

const audioQueue = [];
let isPlayingAudio = false;

const el = (id) => document.getElementById(id);

// Cosmetic-only, frontend-side copy — not scenario data from the API.
// Kept spoiler-free on purpose; falls back to a generic line for any
// scenario not explicitly listed here.
const SCENARIO_OBJECTIVES = {
  'DP-001': 'Help a frustrated administrator regain access while following the required identity verification and security workflow.',
};
const DEFAULT_OBJECTIVE = 'Handle this support case following the required process and communication standards.';

const AREA_ICONS = {
  'Data Protection': '<svg width="26" height="26" viewBox="0 0 24 24" fill="none"><path d="M12 3l7 3v5c0 4.6-3 8.4-7 9.6-4-1.2-7-5-7-9.6V6l7-3Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M9 12l2 2 4-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};
const DEFAULT_AREA_ICON = '<svg width="26" height="26" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/><path d="M12 8v4l3 2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';

async function loadScenarios() {
  const res = await fetch('/api/scenarios');
  const scenarios = await res.json();
  const list = el('scenario-list');
  list.innerHTML = '';
  scenarios.forEach((s) => {
    const card = document.createElement('div');
    card.className = 'scenario-card';
    const icon = AREA_ICONS[s.area] || DEFAULT_AREA_ICON;
    const objective = SCENARIO_OBJECTIVES[s.scenario_id] || DEFAULT_OBJECTIVE;
    card.innerHTML = `
      <div class="scenario-card-top">
        <div class="scenario-icon">${icon}</div>
        <div class="scenario-heading">
          <div class="scenario-eyebrow">${escapeHtml(s.area)}</div>
          <h2>${escapeHtml(s.scenario_id)}</h2>
          <p class="scenario-process">${escapeHtml(s.process_name)}</p>
        </div>
      </div>
      <div class="scenario-facts">
        <div class="fact"><span class="fact-label">Difficulty</span><span class="fact-value fact-pill">${escapeHtml(s.difficulty)}</span></div>
        <div class="fact"><span class="fact-label">Duration</span><span class="fact-value">${s.duration_minutes} min</span></div>
        <div class="fact fact-wide"><span class="fact-label">Skills</span><span class="fact-value">Process &middot; Communication &middot; Customer Handling</span></div>
      </div>
      <div class="scenario-objective">
        <span class="objective-label">Scenario objective</span>
        <p>${escapeHtml(objective)}</p>
      </div>
      <button class="start-call-btn" type="button">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6.6 10.8a13.6 13.6 0 0 0 6.6 6.6l2.2-2.2a1 1 0 0 1 1-.25c1.1.36 2.3.56 3.5.56a1 1 0 0 1 1 1V20a1 1 0 0 1-1 1C10.9 21 3 13.1 3 3.5a1 1 0 0 1 1-1H7.5a1 1 0 0 1 1 1c0 1.2.2 2.4.56 3.5a1 1 0 0 1-.25 1L6.6 10.8Z" fill="currentColor"/></svg>
        Start Call
      </button>
      <p class="scenario-time-note">You'll have up to ${s.duration_minutes} minutes to resolve the case.</p>
    `;
    card.querySelector('.start-call-btn').addEventListener('click', () => startCall(s));
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
  state.transcriptLog.push({ speaker, text });

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

// Drives both the status text and the visual/animation state of the
// customer avatar — purely presentational, mirrors states that already
// exist in the app's message/recording flow.
function setCallState(stateName, statusText, subText) {
  const visual = el('customer-visual');
  visual.className = `customer-visual state-${stateName}`;
  setStatus(statusText);
  el('call-substatus').textContent = subText || '';
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
  state.currentScenario = scenario;
  state.transcriptLog = [];

  showView('view-call');
  el('call-scenario-name').textContent = `${scenario.area} — ${scenario.scenario_id}`;
  el('call-scenario-meta').textContent = scenario.difficulty;
  el('transcript').innerHTML = '';
  setCallState('connecting', 'Connecting…', 'Setting up your call…');
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
    setCallState('idle', 'Microphone access denied', 'Allow microphone access in your browser and try again.');
    return;
  }

  setBaseRemaining(scenario.duration_minutes * 60);

  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  state.ws = new WebSocket(`${protocol}://${location.host}/ws/call/${scenario.scenario_id}`);
  state.ws.binaryType = 'arraybuffer';

  state.ws.addEventListener('open', () => setCallState('connecting', 'Waiting for customer…', ''));
  state.ws.addEventListener('message', handleWsMessage);
  state.ws.addEventListener('close', stopAllTicking);
  state.ws.addEventListener('error', () => setCallState('idle', 'Connection error.', ''));
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
      setCallState('customer-speaking', 'Customer is speaking…', 'Listen carefully and respond naturally.');
      setTalkEnabled(false);
      break;
    case 'trainee_turn_transcribed':
      addTranscriptLine('trainee', msg.text);
      setCallState('processing', 'Processing…', 'The customer is reviewing what you said.');
      break;
    case 'call_ended':
      onCallEnded();
      break;
    case 'evaluating':
      setCallState('processing', 'Evaluating your call…', 'This will just take a moment.');
      break;
    case 'evaluation_result':
      showEvaluation(msg.result);
      break;
    case 'error':
      setCallState('idle', `Error: ${msg.message}`, '');
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
  setCallState('your-turn', 'Your turn', 'Click the microphone and speak naturally.');
  setTalkEnabled(true);
}

function beginRecording() {
  if (!state.stream || el('talk-btn').disabled || state.isRecording) return;
  state.isRecording = true;
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
  el('talk-btn-label').textContent = 'Click to Stop';
  setCallState('listening', 'You’re speaking…', 'Click the microphone again when you’re done.');
  startRecordingTicking();
}

function endRecording() {
  if (!state.isRecording) return;
  state.isRecording = false;
  if (state.mediaRecorder && state.mediaRecorder.state === 'recording') {
    state.mediaRecorder.stop();
  }
  el('talk-btn').classList.remove('recording');
  el('talk-btn-label').textContent = 'Click to Talk';
  stopRecordingTicking();
}

function toggleRecording() {
  if (state.isRecording) {
    endRecording();
  } else {
    beginRecording();
  }
}

async function onRecordingStop() {
  const blob = new Blob(state.audioChunks, { type: state.mediaRecorder.mimeType || 'audio/webm' });
  if (blob.size < 500) {
    setCallState('your-turn', 'Your turn', 'Click the microphone and speak naturally.');
    return;
  }
  setTalkEnabled(false);
  setCallState('processing', 'Sending…', 'Sending your response.');
  const buffer = await blob.arrayBuffer();
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(buffer);
  }
}

function endCall() {
  if (state.isRecording) endRecording();
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
  setCallState('processing', 'Time is passing…', 'Placing the follow-up call.');
}

function onCallEnded() {
  stopAllTicking();
  if (state.stream) {
    state.stream.getTracks().forEach((t) => t.stop());
  }
  setTalkEnabled(false);
  setCallState('processing', 'Call ended — evaluating…', '');
}

// ============ EVALUATION SCREEN ============

const SCORE_RING_CIRCUMFERENCE = 2 * Math.PI * 70;

function bandTone(score) {
  if (score >= 85) return 'tone-success';
  if (score >= 60) return 'tone-warning';
  return 'tone-danger';
}

function animateCountUp(elementId, targetValue, duration = 700) {
  const target = el(elementId);
  const start = performance.now();
  function tick(now) {
    const progress = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    target.textContent = Math.round(targetValue * eased);
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

function showEvaluation(result) {
  showView('view-eval');

  const tone = bandTone(result.overall_score);

  const ringFill = el('score-ring-fill');
  ringFill.setAttribute('stroke-dasharray', `${SCORE_RING_CIRCUMFERENCE}`);
  ringFill.setAttribute('stroke-dashoffset', `${SCORE_RING_CIRCUMFERENCE}`);
  ringFill.classList.remove('tone-success', 'tone-warning', 'tone-danger');
  ringFill.classList.add(tone);
  requestAnimationFrame(() => {
    const offset = SCORE_RING_CIRCUMFERENCE * (1 - result.overall_score / 100);
    ringFill.style.transition = 'stroke-dashoffset 900ms cubic-bezier(0.16, 1, 0.3, 1)';
    ringFill.setAttribute('stroke-dashoffset', `${offset}`);
  });
  animateCountUp('eval-score-number', result.overall_score);

  const scoreValueEl = document.querySelector('.score-ring-value');
  scoreValueEl.classList.remove('tone-success', 'tone-warning', 'tone-danger');
  scoreValueEl.classList.add(tone);

  el('eval-band').textContent = result.band;
  el('eval-band').className = `eval-band ${tone}`;
  el('eval-stars').textContent = '★'.repeat(result.csat_stars) + '☆'.repeat(5 - result.csat_stars);
  el('eval-stars-label').textContent = `Customer Experience — ${result.csat_stars}/5`;
  el('eval-recommendation').textContent = result.recommendation;

  const categoryIcons = {
    'Process & KB': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M9 3h6l1 3h3v3l-3 1v8a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1v-8l-3-1V6h3l1-3Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
    'Communication': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M4 5h16v10H8l-4 4V5Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
    'Fluency': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M4 12h2l2-6 3 12 2-8 2 4h5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    'Pronunciation': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><rect x="9" y="3" width="6" height="11" rx="3" fill="currentColor"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
    'Call Management': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M21 16.4v2.3a1.3 1.3 0 0 1-1.4 1.3 17.6 17.6 0 0 1-7.6-2.7 17.4 17.4 0 0 1-5.3-5.3A17.6 17.6 0 0 1 4 4.4 1.3 1.3 0 0 1 5.3 3h2.3a1.3 1.3 0 0 1 1.3 1.1c.1.9.3 1.8.6 2.6a1.3 1.3 0 0 1-.3 1.4L8 9.3a14 14 0 0 0 5.3 5.3l1.2-1.2a1.3 1.3 0 0 1 1.4-.3c.8.3 1.7.5 2.6.6a1.3 1.3 0 0 1 1.1 1.3Z" fill="currentColor"/></svg>',
  };

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
    const pct = Math.round((value / max) * 100);
    const card = document.createElement('div');
    card.className = 'category-card';
    card.innerHTML = `
      <div class="category-card-top">
        <span class="category-icon">${categoryIcons[label] || ''}</span>
        <span class="category-name">${label}</span>
      </div>
      <div class="category-score">${value}<span class="category-score-max"> / ${max}</span></div>
      <div class="bar-track"><div class="bar-fill" style="width:0%" data-target="${pct}%"></div></div>
    `;
    catsEl.appendChild(card);
  });
  requestAnimationFrame(() => {
    catsEl.querySelectorAll('.bar-fill').forEach((bar) => {
      bar.style.transition = 'width 700ms cubic-bezier(0.16, 1, 0.3, 1)';
      bar.style.width = bar.dataset.target;
    });
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
    errorsEl.hidden = true;
  } else {
    errorsEl.hidden = false;
    allErrors.forEach((e) => {
      const div = document.createElement('div');
      div.className = `err-item err-${e.sevLabel}`;
      const label = e.sevLabel === 'critical' ? 'Critical process issue' : 'Major process issue';
      div.innerHTML = `
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3 2 20h20L12 3Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M12 10v4M12 17h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
        <div><span class="err-label">${label}</span><p>${escapeHtml(e.description)}</p></div>
      `;
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

function practiceAgain() {
  if (state.ws) {
    try { state.ws.close(); } catch (err) { /* already closed */ }
  }
  stopAllTicking();
  if (state.currentScenario) {
    startCall(state.currentScenario);
  } else {
    showView('view-picker');
  }
}

function openTranscriptModal() {
  const body = el('transcript-modal-body');
  body.innerHTML = '';
  if (state.transcriptLog.length === 0) {
    body.innerHTML = '<p class="modal-empty">No transcript available for this call.</p>';
  } else {
    state.transcriptLog.forEach(({ speaker, text }) => {
      const div = document.createElement('div');
      div.className = `line ${speaker}`;
      const label = speaker === 'customer' ? 'Customer' : 'You';
      div.innerHTML = `<span class="speaker">${label}</span>${escapeHtml(text)}`;
      body.appendChild(div);
    });
  }
  el('transcript-modal').hidden = false;
}

function closeTranscriptModal() {
  el('transcript-modal').hidden = true;
}

el('talk-btn').addEventListener('click', toggleRecording);

el('time-skip-btn').addEventListener('click', requestTimeSkip);
el('end-call-btn').addEventListener('click', endCall);
el('retry-btn').addEventListener('click', practiceAgain);
el('back-btn').addEventListener('click', resetToPicker);
el('transcript-btn').addEventListener('click', openTranscriptModal);
el('transcript-close-btn').addEventListener('click', closeTranscriptModal);
el('transcript-modal').addEventListener('click', (e) => {
  if (e.target.id === 'transcript-modal') closeTranscriptModal();
});

loadScenarios();
