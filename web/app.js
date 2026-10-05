const $ = (id) => document.getElementById(id);
let state = null;
let busy = false;
let running = false;
let timer = null;
let sourceDirty = false;
let activeButton = null;
let controlQueue = Promise.resolve();
let topAuto = true;
let clockSelectionHz = 100000000;
let backendOffline = false;
let checkingBackend = false;

async function checkBackend() {
  if (checkingBackend) return;
  checkingBackend = true;
  $('retry-backend').disabled = true;
  try {
    const response = await fetch('/healthz', {cache: 'no-store', signal: AbortSignal.timeout(7000)});
    if (!response.ok || !(await response.json()).ok) throw new Error('Server unavailable');
    if (backendOffline || !state) await loadWorkbench(true);
    backendOffline = false;
  } catch {
    backendOffline = true;
    running = false;
    clearInterval(timer);
    timer = null;
    $('build-status').textContent = 'Server offline';
  } finally {
    $('backend-offline').hidden = !backendOffline;
    $('retry-backend').disabled = false;
    checkingBackend = false;
    renderControls();
  }
}

const boardPositions = {
  basys3: {
    width:500, height:313,
    switches: [442,416,391,365,340,315,289,264,237,212,186,161,135,109,83,59],
    switchY:276, ledY:247, ledX:(x)=>x+2,
    buttons:{btnC:[360,195],btnU:[360,167],btnD:[360,223],btnL:[330,195],btnR:[395,195]},
    image:'/basys3.jpg'
  },
  nexys_a7_100t: {
    width:1200, height:1200,
    switches:Array.from({length:16},(_,i)=>954-i*49), switchY:910, ledY:832, ledX:(x)=>x,
    buttons:{btnC:[844,655],btnU:[844,586],btnD:[844,726],btnL:[776,655],btnR:[910,655]},
    image:'/nexysa7.avif'
  },
  arty_a7_100t: {
    width:600, height:600,
    switches:[351,313,275,237], switchY:470, ledY:483, ledX:(_,i)=>182-i*35,
    buttons:{btnC:[536,470],btnU:[494,470],btnD:[450,470],btnL:[407,470]},
    image:'/artya7.png'
  }
};
const buttonNames = {btnC:'Center',btnU:'Up',btnD:'Down',btnL:'Left',btnR:'Right'};
let renderedBoard = null;

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}

function hex(value) { return '0x' + Number(value).toString(16).padStart(4, '0').toUpperCase(); }

function frequencyLabel(hz) {
  if (hz >= 1000000) return `${Number((hz / 1000000).toPrecision(3))} MHz`;
  if (hz >= 1000) return `${Number((hz / 1000).toPrecision(3))} kHz`;
  return `${Number(hz.toPrecision(3))} Hz`;
}

function sliderFrequency(position) {
  return Math.max(1, Math.round(Math.exp(Math.log(25000000) * Number(position) / 100)));
}

function renderClock() {
  const selected = clockSelectionHz;
  const actual = selected === 100000000 ? selected : 50000000 / Math.max(1, Math.round(50000000 / selected));
  $('clock-value').textContent = frequencyLabel(actual);
  $('clock-slider').value = Math.round(100 * Math.log(Math.min(selected, 25000000)) / Math.log(25000000));
  document.querySelectorAll('[data-clock-hz]').forEach(button => {
    const active = Number(button.dataset.clockHz) === selected;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  if (state && selected !== state.clockHz) $('clock-note').textContent = 'Synthesize to apply this clock to the bitstream.';
  else if (state && state.phase !== 'ready' && !state.clockPort) $('clock-note').textContent = 'This design has no clk input; switches still update the preview.';
  else if (actual !== selected) $('clock-note').textContent = `Nearest divider from 100 MHz: ${frequencyLabel(actual)}. Preview playback is capped at 20 cycles/s.`;
  else $('clock-note').textContent = 'Generated from the 100 MHz board input. Preview playback is capped at 20 cycles/s.';
}

function place(element, x, y, positions) {
  element.style.left = `${x / positions.width * 100}%`;
  element.style.top = `${y / positions.height * 100}%`;
}

function setupBoard() {
  const positions = boardPositions[state.board];
  renderedBoard = state.board;
  $('board-image').src = positions.image;
  $('board-image').parentElement.classList.toggle('board-photo-square', state.board !== 'basys3');
  $('board-image').alt = state.board === 'basys3'
    ? 'Top-down photograph of the Digilent Basys 3 FPGA board'
    : `Top-down photograph of the Digilent ${state.boardName} FPGA board`;
  $('board-overlays').replaceChildren();
  for (let i = 0; i < state.switchCount; i++) {
    const x = positions.switches[i];
    const switchButton = document.createElement('button');
    switchButton.type = 'button';
    switchButton.className = 'photo-switch';
    switchButton.dataset.switch = i;
    switchButton.setAttribute('role', 'switch');
    switchButton.setAttribute('aria-label', `Switch ${i}`);
    switchButton.setAttribute('aria-checked', 'false');
    switchButton.title = `SW${i}`;
    place(switchButton, x, positions.switchY, positions);
    switchButton.addEventListener('click', () => toggleSwitch(i));
    $('board-overlays').append(switchButton);
    const led = document.createElement('span');
    led.className = 'photo-led';
    led.dataset.led = i;
    led.setAttribute('aria-label', `LED ${i}`);
    place(led, positions.ledX(x,i), positions.ledY, positions);
    $('board-overlays').append(led);
  }
  for (const [name, [x,y]] of Object.entries(positions.buttons)) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'photo-button';
    button.dataset.button = name;
    const label = state.board === 'arty_a7_100t'
      ? `BTN${['btnC','btnU','btnD','btnL'].indexOf(name)}` : buttonNames[name];
    button.title = `${label} pushbutton`;
    button.setAttribute('aria-label', `${label} pushbutton`);
    place(button, x, y, positions);
    button.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      button.setPointerCapture(event.pointerId);
      pressButton(name, true);
    });
    for (const type of ['pointerup','pointercancel','lostpointercapture'])
      button.addEventListener(type, () => pressButton(name, false));
    button.addEventListener('keydown', event => {
      if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); pressButton(name, true); }
    });
    button.addEventListener('keyup', event => {
      if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); pressButton(name, false); }
    });
    button.addEventListener('blur', () => pressButton(name, false));
    $('board-overlays').append(button);
  }
}

function showNotice(message, type='error') {
  const notice = $('notice');
  notice.textContent = message;
  notice.className = `notice ${type}`;
  notice.hidden = false;
  clearTimeout(showNotice.timeout);
  showNotice.timeout = setTimeout(() => { notice.hidden = true; }, 6500);
}

async function requestJSON(path, options) {
  let response;
  try {
    response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', ...options});
  } catch {
    throw new Error('Cannot reach the FPGA backend. Check that the backend is running and the /api route is configured.');
  }
  if (!response.headers.get('Content-Type')?.toLowerCase().includes('application/json')) {
    if (response.status === 404) {
      throw new Error('FPGA backend not connected. Configure Vercel to forward /api requests to your running backend.');
    }
    throw new Error(`FPGA backend returned a non-JSON response (HTTP ${response.status}). Check the backend URL and service logs; a sleeping service may need time to start.`);
  }
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(`FPGA backend returned invalid JSON (HTTP ${response.status}). Check the backend service logs.`);
  }
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}

async function api(path, payload) {
  return requestJSON(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
}

async function action(path, payload) {
  if (busy || backendOffline) return false;
  busy = true;
  renderControls();
  const progress = {'/api/synthesize':'Synthesizing Verilog…','/api/implement':'Placing, routing, and preparing board…','/api/bitstream':'Generating Artix-7 bitstream…'}[path];
  if (progress) { $('build-status').textContent = progress; $('build-log').textContent = progress + '\nThe tool result will appear here when it finishes.'; }
  try {
    const result = await api(path, payload);
    state = result;
    if (path === '/api/synthesize') { $('top-module').value = result.top; topAuto = true; }
    render();
    return true;
  } catch (error) {
    showNotice(error.message);
    $('build-log').textContent = error.message;
    try { state = await requestJSON('/api/status'); render(); } catch {}
    return false;
  } finally {
    busy = false;
    renderControls();
  }
}

function control(payload) {
  if (backendOffline) return;
  if (!state?.preview) { showNotice('Synthesize, then choose switches with Implement to start the board preview.'); return; }
  controlQueue = controlQueue.then(async () => {
    if (backendOffline) return;
    state = await api('/api/control', typeof payload === 'function' ? payload() : payload);
    render();
  }).catch(error => { stopRun(); showNotice(error.message); });
  return controlQueue;
}

function toggleSwitch(index) {
  if (!state?.preview) { showNotice('Choose switches with Implement first.'); return; }
  control(() => ({switches: state.switches ^ (1 << index)}));
}

function pressButton(name, pressed) {
  if (pressed && activeButton === name) return;
  if (!pressed && activeButton !== name) return;
  activeButton = pressed ? name : null;
  control({button:name,value:pressed});
}

function stopRun() {
  running = false;
  clearInterval(timer);
  timer = null;
  renderControls();
}

function toggleRun() {
  if (!state?.preview) { showNotice('Start the board preview with Implement first.'); return; }
  if (running) return stopRun();
  running = true;
  renderControls();
  let inFlight = false;
  const playbackHz = Math.min(20, Math.max(1, state.clockActualHz || 20));
  timer = setInterval(async () => {
    if (inFlight || !running) return;
    inFlight = true;
    await control({step:1});
    inFlight = false;
  }, 1000 / playbackHz);
}

function renderControls() {
  const unavailable = busy || backendOffline;
  const synthesized = ['synthesized','preview','implemented','bitstream'].includes(state?.phase);
  $('board-select').disabled = unavailable;
  $('synthesize-button').disabled = unavailable;
  $('implement-button').disabled = unavailable || !synthesized || sourceDirty;
  $('bitstream-button').disabled = unavailable || !['implemented','bitstream'].includes(state?.phase) || sourceDirty;
  $('run-button').disabled = unavailable || !state?.preview;
  $('step-button').disabled = unavailable || !state?.preview;
  $('reset-button').disabled = unavailable || !state?.preview || !state?.boardButtons?.includes('btnC');
  $('reset-button').title = state?.board === 'arty_a7_100t' ? 'Pulse Arty BTN0' : 'Pulse center pushbutton';
  $('run-button').innerHTML = running ? '<span aria-hidden="true">■</span> Pause' : '<span aria-hidden="true">▶</span> Run';
  $('editor-state').textContent = sourceDirty ? 'Unsynthesized changes' : 'Saved in this browser';
  $('editor-state').classList.toggle('dirty', sourceDirty);
}

function renderStages() {
  const phase = state.phase;
  $('stage-synth').classList.toggle('done', ['synthesized','preview','implemented','bitstream'].includes(phase));
  $('stage-implement').classList.toggle('done', ['implemented','bitstream'].includes(phase));
  $('stage-bitstream').classList.toggle('done', phase === 'bitstream');
  $('stage-synth').classList.toggle('active', phase === 'ready');
  $('stage-implement').classList.toggle('active', phase === 'synthesized' || phase === 'preview');
  $('stage-bitstream').classList.toggle('active', phase === 'implemented');
  $('stage-synth').classList.toggle('stale', sourceDirty);
}

function renderBoard() {
  if (renderedBoard !== state.board) setupBoard();
  const word = state.switches;
  const ledString = state.displayLeds || state.leds || 'xxxx';
  const number = /^[0-9a-f]{4}$/i.test(ledString) ? parseInt(ledString,16) : null;
  $('switch-value').textContent = hex(word);
  $('led-value').textContent = number === null ? '0x----' : hex(number);
  $('cycle-count').textContent = String(state.cycles);
  $('cell-count').textContent = state.gates === null ? '—' : String(state.gates);
  $('pin-map-summary').textContent = state.phase === 'ready'
    ? 'Synthesize to see your signal mapping.' : state.mappingSummary;
  const boardStatus = state.fabricStatus === 'ready' ? 'BITSTREAM FABRIC' :
    state.fabricStatus === 'decoding' ? 'DECODING BITSTREAM' :
    state.fabricStatus === 'error' ? 'RTL PREVIEW · DECODER ERROR' :
    state.phase === 'bitstream' && (state.board !== 'basys3' || state.clockHz !== 100000000) ? 'BITSTREAM READY · RTL VIEW' :
    state.preview ? 'RTL PREVIEW' : 'AWAITING DESIGN';
  $('board-status').textContent = boardStatus;
  $('board-status').classList.toggle('live', Boolean(state.preview));
  if (state.phase === 'bitstream' && state.board !== 'basys3')
    $('fabric-note').textContent = 'Bitstream ready. LEDs and waveform show RTL simulation; fabric decoding is available for Basys 3.';
  else if (state.phase === 'bitstream' && state.clockHz !== 100000000)
    $('fabric-note').textContent = 'Bitstream ready. LEDs and waveform show selected clock cycles in RTL preview.';
  else if (state.fabricError) $('fabric-note').textContent = `Fabric decoder: ${state.fabricError}`;
  else $('fabric-note').textContent = state.fabricStatus === 'ready'
    ? 'Board LEDs and pin traces are driven by the decoded bitstream.'
    : state.preview ? 'Board LEDs and traces are driven by RTL simulation.'
    : 'Synthesize your Verilog to begin.';
  const switches = Array.from({length:state.switchCount}, (_,i) => i);
  $('switch-grid').innerHTML = switches.map(i => `<button type="button" class="switch-tile ${(word>>i)&1?'on':''}" data-switch="${i}" role="switch" aria-checked="${Boolean((word>>i)&1)}" aria-label="Switch ${i}"><span class="switch-track"><span></span></span><small>SW${i}</small></button>`).join('');
  document.querySelectorAll('.photo-switch').forEach(button => {
    const on = Boolean((word >> Number(button.dataset.switch)) & 1);
    button.classList.toggle('on', on);
    button.setAttribute('aria-checked', String(on));
  });
  document.querySelectorAll('.photo-led').forEach(led => {
    const index = Number(led.dataset.led);
    led.classList.toggle('on', number !== null && Boolean((number>>index)&1));
    led.classList.toggle('unknown', number === null);
  });
  document.querySelectorAll('.photo-button').forEach(button => {
    button.disabled = !state.preview || !state.boardButtons.includes(button.dataset.button);
  });
  const leds = Array.from({length:state.ledCount}, (_,i) => i);
  $('led-grid').innerHTML = leds.map(i => `<div class="led-tile"><span class="led-bulb ${number !== null && (number>>i)&1?'on':''} ${number===null?'unknown':''}"></span><small>LD${i}</small></div>`).join('');
}

function renderWaveform() {
  const decoded = state.fabricStatus === 'ready' && state.fabricWaveform?.samples?.length;
  let labels, values;
  if (decoded) {
    labels = state.fabricWaveform.signals;
    values = state.fabricWaveform.samples;
  } else {
    labels = ['SW0','SW1','BTN C',...Array.from({length:Math.min(state.ledCount,8)},(_,i)=>`LED${i}`)];
    values = state.samples.map(sample => {
      const led = /^[0-9a-f]{4}$/i.test(sample.leds) ? parseInt(sample.leds,16) : null;
      return [sample.switches&1, (sample.switches>>1)&1, sample.buttons&1,
        ...Array.from({length:Math.min(state.ledCount,8)}, (_,i) => led === null ? null : (led>>i)&1)];
    });
  }
  if (!values.length) {
    $('waveform').innerHTML = '<div class="wave-empty"><span>〰</span><strong>No samples yet</strong><p>Implement the design to capture the board signals.</p></div>';
    $('wave-count').textContent = '0 samples';
    $('wave-source').textContent = 'Waiting for simulation';
    $('download-waveform').disabled = true;
    return;
  }
  $('download-waveform').disabled = false;
  const samples = values.slice(-48);
  const displayLabels = labels.slice(0, 12);
  const h = 30 + displayLabels.length * 26;
  const w = 660;
  const left = 72;
  const plotW = w-left-12;
  const dx = plotW/Math.max(samples.length, 12);
  const colors = ['#26c8ec','#2baed8','#d99b54','#50d5a3','#50d5a3','#50d5a3','#50d5a3','#50d5a3','#50d5a3','#50d5a3','#50d5a3','#50d5a3'];
  let svg = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Digital signal waveform, ${samples.length} samples" preserveAspectRatio="none">`;
  for (let tick=0;tick<=8;tick++) {
    const x = left + tick*plotW/8;
    svg += `<line class="wave-grid" x1="${x}" y1="21" x2="${x}" y2="${h-4}"/><text class="wave-axis" x="${x+3}" y="14">${Math.round(tick*samples.length/8)}</text>`;
  }
  for (let row=0; row<displayLabels.length; row++) {
    const y=28+row*26;
    svg += `<line class="wave-row" x1="0" y1="${y+25}" x2="${w}" y2="${y+25}"/><text class="wave-label" x="10" y="${y+17}">${escapeHtml(displayLabels[row])}</text>`;
    let d='';
    samples.forEach((sample,index) => {
      const value=sample[row];
      const yy=value===null?y+13:value?y+5:y+21;
      const x=left+index*dx;
      if (index===0) d=`M ${x} ${yy}`;
      else d+=` H ${x} V ${yy}`;
    });
    d+=` H ${left+samples.length*dx}`;
    svg += `<path d="${d}" fill="none" stroke="${colors[row%colors.length]}" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`;
  }
  $('waveform').innerHTML = svg+'</svg>';
  $('wave-count').textContent = `${values.length} samples`;
  $('wave-source').textContent = decoded ? 'Decoded bitstream pins' : 'RTL simulation pins';
}

function renderArtifacts() {
  const labels = {'design.v':'Verilog source','adapter.v':'Generated board adapter','clock_adapter.v':'Generated clock divider','board.xdc':'Board pin constraints','netlist.json':'Synthesized netlist',
    'xc7_netlist.json':'Artix-7 mapped netlist','routed.json':'Placed and routed design','design.fasm':'FPGA feature map',
    'design.frames':'Configuration frames','route.log':'Place and route log',
    'synth.rpt':'Synthesis report','timing.rpt':'Timing report','routed.dcp':'Routed checkpoint','design.bit':'FPGA bitstream'};
  const icons = {'design.bit':'◈','routed.dcp':'▣','routed.json':'▣','netlist.json':'◇'};
  $('artifact-list').innerHTML = state.artifacts.length
    ? state.artifacts.map(name => `<a class="artifact" href="/api/artifact/${encodeURIComponent(name)}" download><span class="artifact-icon">${icons[name]||'▤'}</span><span><strong>${escapeHtml(name)}</strong><small>${labels[name]||'Build artifact'}</small></span><span class="download-icon">↓</span></a>`).join('')
    : '<div class="artifact-empty">Artifacts appear after synthesis.</div>';
}

function render() {
  if (!state) return;
  $('board-select').value = state.board;
  $('board-title').textContent = `Virtual ${state.boardName}`;
  $('brand-subtitle').textContent = `Verilog lab for ${state.boardName}`;
  $('top-part').textContent = `${state.boardName.toUpperCase()} · ${state.part.toUpperCase()}`;
  document.title = `FPGA Workbench · ${state.boardName}`;
  renderControls();
  renderClock();
  renderStages();
  renderBoard();
  renderWaveform();
  renderArtifacts();
  $('build-log').textContent = state.log || 'Ready.';
  $('tool-summary').textContent = state.tools.vivado ? 'Vivado connected · bitstream available' :
    state.tools.openxc7 ? 'Open XC7 connected · bitstream available' : 'Yosys + Icarus preview · install open XC7 for .bit';
  $('tool-summary').title = state.tools.vivado ? 'Vivado detected on this server' :
    state.tools.openxc7 ? 'Yosys, nextpnr-xilinx, and Project X-Ray are ready' : `Install the open XC7 database for ${state.part} to place, route, and generate a bitstream`;
  $('build-status').textContent = state.phase === 'bitstream' ? 'Bitstream ready' :
    state.phase === 'implemented' ? 'Implementation complete' :
    state.phase === 'preview' ? 'RTL preview ready' :
    state.phase === 'synthesized' ? 'Synthesis complete' : 'Ready to build';
}

function updateLines() {
  const lines = $('editor').value.split('\n').length;
  $('line-numbers').textContent = Array.from({length:lines}, (_,i) => i+1).join('\n');
  $('editor-lines').textContent = `${lines} lines`;
}

function suggestTop() {
  if (!topAuto) return;
  const uncommented = $('editor').value.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, '');
  const modules = [...uncommented.matchAll(/\bmodule\s+(?:(?:automatic|static)\s+)?([A-Za-z_][A-Za-z_0-9]*)\b/g)];
  if (modules.length === 1) $('top-module').value = modules[0][1];
}

function setupEditor() {
  document.querySelectorAll('[data-clock-hz]').forEach(button => button.addEventListener('click', () => {
    stopRun();
    clockSelectionHz = Number(button.dataset.clockHz);
    sourceDirty = true;
    renderClock();
    renderControls();
  }));
  $('clock-slider').addEventListener('input', event => {
    stopRun();
    clockSelectionHz = sliderFrequency(event.target.value);
    sourceDirty = true;
    renderClock();
    renderControls();
  });
  $('board-select').addEventListener('change', async () => {
    stopRun();
    const requested = $('board-select').value;
    if (await action('/api/board', {board:requested}))
      showNotice(`${state.boardName} selected. Synthesize this design for the new board.`, 'success');
  });
  $('editor').addEventListener('input', () => {
    sourceDirty = true;
    suggestTop();
    updateLines();
    renderControls();
    try { localStorage.setItem('fpga-workbench-private-code-v1', $('editor').value); } catch {}
  });
  $('editor').addEventListener('scroll', () => { $('line-numbers').scrollTop = $('editor').scrollTop; });
  $('editor').addEventListener('keydown', (event) => {
    if (event.key === 'Tab') {
      event.preventDefault();
      const field = event.target;
      const start = field.selectionStart;
      field.setRangeText('    ', start, field.selectionEnd, 'end');
      field.dispatchEvent(new Event('input'));
    }
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault(); $('synthesize-button').click();
    }
  });
  $('top-module').addEventListener('input', () => { topAuto=false; sourceDirty=true; renderControls(); });
  $('synthesize-button').addEventListener('click', async () => {
    stopRun();
    const success=await action('/api/synthesize', {code:$('editor').value, top:$('top-module').value.trim(), clockHz:clockSelectionHz});
    if (success) { sourceDirty=false; renderControls(); showNotice('Synthesis completed. Choose switches for implementation.', 'success'); }
  });
  $('implement-button').addEventListener('click', () => {
    if (state?.tools.vivado) $('implement-mode').textContent = 'Vivado will place and route the design. Your switch word sets the initial virtual board inputs.';
    else if (state?.tools.openxc7) $('implement-mode').textContent = 'Open XC7 will place and route the design. Your switch word sets the initial virtual board inputs.';
    else $('implement-mode').textContent = 'The board will run an RTL preview. Install open XC7 to enable placement, routing, and bitstream generation.';
    $('implement-mode').textContent += ` Mapping: ${state.mappingSummary}`;
    $('switch-dialog-description').textContent = `The ${state.switchCount}-bit switch word sets the starting input for ${state.boardName}. You can change switches live after implementation.`;
    $('switch-word').value = hex(state.switches);
    renderDialogSwitches();
    $('switch-dialog').showModal();
  });
  $('bitstream-button').addEventListener('click', async () => {
    if (await action('/api/bitstream', {})) showNotice('Bitstream generated. Download design.bit from Artifacts.', 'success');
  });
  $('switch-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const raw = $('switch-word').value.trim().replace(/^0x/i,'');
    if (!/^[0-9a-f]{1,4}$/i.test(raw) || parseInt(raw,16) >= (1 << state.switchCount)) {
      $('switch-error').textContent = `Enter a ${state.switchCount}-bit hexadecimal value.`; return;
    }
    $('switch-error').textContent='';
    $('switch-dialog').close();
    if (await action('/api/implement', {switches:parseInt(raw,16)}))
      showNotice(state.phase==='implemented'?'Implementation complete.':'RTL board preview ready.', 'success');
  });
  $('switch-word').addEventListener('input', renderDialogSwitches);
  $('dialog-switches').addEventListener('click', (event) => {
    const button=event.target.closest('[data-bit]');
    if (!button) return;
    const current=parseInt($('switch-word').value.replace(/^0x/i,''),16)||0;
    $('switch-word').value=hex(current^(1<<Number(button.dataset.bit)));
    renderDialogSwitches();
  });
  $('run-button').addEventListener('click', toggleRun);
  $('step-button').addEventListener('click', () => control({step:1}));
  $('download-source').addEventListener('click', () => {
    const blob=new Blob([$('editor').value], {type:'text/plain'});
    const link=document.createElement('a'); link.href=URL.createObjectURL(blob); link.download=`${$('top-module').value.trim()||'design'}.v`; link.click();
    setTimeout(()=>URL.revokeObjectURL(link.href),1000);
  });
  $('download-waveform').addEventListener('click', () => {
    const decoded = state.fabricStatus === 'ready' && state.fabricWaveform?.samples?.length;
    const labels = decoded ? state.fabricWaveform.signals : ['SW0','SW1','BTN_C',...Array.from({length:Math.min(state.ledCount,8)},(_,i)=>`LED${i}`)];
    const values = decoded ? state.fabricWaveform.samples : state.samples.map(sample => {
      const led = /^[0-9a-f]{4}$/i.test(sample.leds) ? parseInt(sample.leds,16) : null;
      return [sample.switches&1,(sample.switches>>1)&1,sample.buttons&1,
        ...Array.from({length:Math.min(state.ledCount,8)},(_,i)=>led===null?'X':(led>>i)&1)];
    });
    const csv = ['cycle,'+labels.join(','),...values.map((row,i)=>[i,...row.map(value=>value===null?'X':value)].join(','))].join('\n')+'\n';
    const blob = new Blob([csv],{type:'text/csv'});
    const link = document.createElement('a');link.href=URL.createObjectURL(blob);link.download='waveform.csv';link.click();
    setTimeout(()=>URL.revokeObjectURL(link.href),1000);
  });
}

function renderDialogSwitches() {
  const value=parseInt($('switch-word').value.replace(/^0x/i,''),16)||0;
  $('dialog-switches').innerHTML=Array.from({length:state.switchCount},(_,i)=>`<button type="button" data-bit="${i}" class="dialog-switch ${(value>>i)&1?'on':''}" aria-pressed="${Boolean((value>>i)&1)}"><span>${(value>>i)&1}</span><small>SW${i}</small></button>`).join('');
}

async function loadWorkbench(preserveEditor = false) {
  const keepEdits = preserveEditor && (state !== null || $('editor').value.length > 0);
  await requestJSON('/api/session');
  const [source,status]=await Promise.all([requestJSON('/api/source'),requestJSON('/api/status')]);
  state=status;
  if (!keepEdits) clockSelectionHz = state.clockHz || 100000000;
  let saved;
  try { saved=localStorage.getItem('fpga-workbench-private-code-v1'); } catch {}
  if (!keepEdits) {
    $('editor').value=saved||source.code;
    $('top-module').value=source.top;
  }
  suggestTop();
  sourceDirty=$('editor').value!==source.code || clockSelectionHz !== state.clockHz;
  updateLines();
  render();
}

async function init() {
  setupEditor();
  $('retry-backend').addEventListener('click', checkBackend);
  setInterval(checkBackend, 30000);
  $('switch-grid').addEventListener('click', (event) => {
    const button = event.target.closest('[data-switch]');
    if (button) toggleSwitch(Number(button.dataset.switch));
  });
  $('reset-button').addEventListener('click', async () => {
    await control({button:'btnC',value:true});
    await control({button:'btnC',value:false});
  });
  setInterval(async () => {
    if (busy || backendOffline || running || !state?.fabricStatus || state.fabricStatus==='error') return;
    try { state=await requestJSON('/api/status'); render(); } catch {}
  }, 900);
  await loadWorkbench();
}

init().catch(error => {
  $('build-status').textContent = 'Backend unavailable';
  $('build-log').textContent = error.message;
  showNotice(`Unable to connect to the workbench: ${error.message}`);
  checkBackend();
});
