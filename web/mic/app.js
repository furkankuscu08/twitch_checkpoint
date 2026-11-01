const device = document.querySelector('#device');
const toggle = document.querySelector('#toggle');
const connection = document.querySelector('#connection');
const error = document.querySelector('#error');
const meter = document.querySelector('#meter');
const level = document.querySelector('#level');
const settings = document.documentElement.dataset;
const retryMin = Number(settings.retryMin) * 1000;
const retryMax = Number(settings.retryMax) * 1000;
let running = false, stream, context, worklet, socket, timer;
let generation = 0, retry = retryMin, lastLevel = 0;

function status(text) {
  connection.textContent = text;
  connection.classList.toggle('connected', text === 'Connected');
}
function rememberedDevice() {
  try { return localStorage.getItem('minefield-mic-device') || ''; } catch { return ''; }
}
async function devices() {
  const selected = device.value || rememberedDevice();
  const inputs = (await navigator.mediaDevices.enumerateDevices()).filter(item => item.kind === 'audioinput');
  device.replaceChildren(new Option('Default microphone', ''));
  inputs.filter(item => item.deviceId && item.deviceId !== 'default').forEach((item, index) => {
    device.add(new Option(item.label || `Microphone ${index + 1}`, item.deviceId));
  });
  if ([...device.options].some(option => option.value === selected)) device.value = selected;
}
function reconnect() {
  clearTimeout(timer);
  if (running && context?.state === 'running') {
    status('Reconnecting…');
    timer = setTimeout(connect, retry);
    retry = Math.min(retry * 2, retryMax);
  }
}
function connect() {
  if (!running || context?.state !== 'running') return;
  const current = new WebSocket(`ws://${location.hostname}:${settings.enginePort}/audio`);
  socket = current;
  status('Connecting…');
  current.onopen = () => {
    if (!running || socket !== current) { current.close(); return; }
    retry = retryMin;
    generation += 1;
    current.send(JSON.stringify({type: 'hello', sample_rate: 16000, client_start_wall: Date.now() / 1000}));
    worklet.port.postMessage({type: 'reset', generation});
    status('Connected');
  };
  current.onclose = () => {
    if (socket !== current) return;
    socket = null;
    generation += 1;
    reconnect();
  };
  current.onerror = () => current.close();
}
async function stop() {
  running = false;
  clearTimeout(timer);
  generation += 1;
  const old = socket; socket = null; old?.close();
  stream?.getTracks().forEach(track => track.stop());
  if (context && context.state !== 'closed') await context.close();
  stream = context = worklet = null;
  meter.setAttribute('aria-valuenow', '0'); level.style.width = '0%';
  document.querySelector('#level-label').textContent = 'Quiet';
  toggle.textContent = 'Start microphone'; toggle.classList.remove('active');
  status('Stopped');
}
async function start() {
  error.textContent = '';
  toggle.disabled = true;
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: {
      deviceId: device.value ? {exact: device.value} : undefined,
      channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false
    }});
    context = new AudioContext();
    await context.audioWorklet.addModule('/mic/worklet.js');
    worklet = new AudioWorkletNode(context, 'minefield-mic', {channelCount: 1, channelCountMode: 'explicit',
      processorOptions: {maxPendingFrames: Number(settings.audioQueue) * 4}});
    context.createMediaStreamSource(stream).connect(worklet);
    worklet.connect(context.destination); // Unwritten outputs are silent; keeps processing active.
    worklet.port.onmessage = ({data}) => {
      worklet?.port.postMessage({type: 'ack', generation: data.generation});
      if (!running || data.generation !== generation) return;
      if (data.gap) { generation += 1; socket?.close(); return; }
      if (performance.now() - lastLevel >= 100) {
        const value = Math.min(100, Math.round(data.level * 300));
        level.style.width = `${value}%`; meter.setAttribute('aria-valuenow', String(value));
        document.querySelector('#level-label').textContent = value ? 'Receiving audio' : 'Quiet';
        lastLevel = performance.now();
      }
      if (socket?.readyState === WebSocket.OPEN) {
        // Do not retain a backlog after a transport stall. Reconnect with fresh audio.
        if (socket.bufferedAmount > 2560) { generation += 1; socket.close(); }
        else socket.send(data.pcm);
      }
    };
    running = true;
    context.onstatechange = () => {
      if (!running) return;
      if (context.state === 'running') { if (!socket) connect(); }
      else { clearTimeout(timer); socket?.close(); status('Paused'); }
    };
    stream.getAudioTracks()[0].onended = () => { stop(); };
    await context.resume();
    toggle.textContent = 'Stop microphone'; toggle.classList.add('active');
    await devices();
    connect();
  } catch {
    await stop();
    error.textContent = 'Microphone unavailable. Allow access in Chrome and check your audio input.';
  } finally { toggle.disabled = false; }
}
toggle.addEventListener('click', () => running ? stop() : start());
device.addEventListener('change', async () => {
  try { localStorage.setItem('minefield-mic-device', device.value); } catch { /* Storage is optional. */ }
  if (running) { await stop(); await start(); }
});
navigator.mediaDevices?.addEventListener('devicechange', () => { devices().catch(() => {}); });
if (navigator.mediaDevices) devices().catch(() => {});
window.addEventListener('pagehide', () => { stop(); });
