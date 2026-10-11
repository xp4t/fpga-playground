(() => {
  const retry = document.getElementById('retry-connection');
  const status = document.getElementById('connection-status');
  let checking = false;
  async function check() {
    if (checking) return;
    checking = true;
    retry.disabled = true;
    status.textContent = 'Checking the FPGA server…';
    try {
      const response = await fetch('/healthz', {cache: 'no-store', signal: AbortSignal.timeout(7000)});
      if (response.ok && (await response.json()).ok) {
        status.textContent = 'Server connected. Opening the workbench…';
        location.replace('/');
        return;
      }
    } catch {}
    status.textContent = 'Still unavailable. Checking again in 30 seconds.';
    checking = false;
    retry.disabled = false;
  }
  retry.addEventListener('click', check);
  setInterval(check, 30000);
})();
