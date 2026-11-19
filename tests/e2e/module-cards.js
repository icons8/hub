// Synthetic data, production modulesSection renderer and styles. No live hub.
(async () => {
  window.addEventListener('error', e => { window.moduleCardsError = e.message; });
  window.addEventListener('unhandledrejection', e => { window.moduleCardsError = String(e.reason); });
  const {modulesSection} = await import('/static/js/modules.js');
  const {S} = await import('/static/js/state.js');
  S.snap ||= {settings: []};
  const response = {
    modules: [{id: 'example', title: 'Example'.repeat(18), state: 'off', toggle: true,
      enabled: false, detail: 'A long module description with ordinary words.',
      command: 'example --path=' + 'directory/'.repeat(28)}],
    installed: [{name: 'extension-'.repeat(15), version: '1.0.0', state: 'disabled',
      enabled: false, origin: 'git', source: 'https://example.invalid/' + 'path/'.repeat(25),
      ref: 'main', api: '^2.3', halt: 'ok', capabilities: ['http']}],
  };
  const fetch = window.fetch;
  window.fetch = async (url, ...args) => {
    if (url === '/api/modules') return {json: async () => response};
    return fetch(url, ...args); // other requests stay on the isolated test server
  };
  const host = document.createElement('div');
  host.style.padding = '16px';
  document.querySelector('.app').style.display = 'none';
  document.body.append(host);
  const mount = () => host.replaceChildren(modulesSection());
  window.addEventListener('hub-modules', mount);
  mount();
  await new Promise(resolve => {
    const check = () => host.querySelector('.module-card') ? resolve() : requestAnimationFrame(check);
    check();
  });
  const cards = [...host.querySelectorAll('.module-card')];
  for (const card of cards) card.querySelector('.module-toggle').textContent = 'Enable this example module';
  // Intentional inner scroll content must stay scrollable, not clipped or force-wrapped.
  const code = document.createElement('pre');
  code.id = 'unwrapped-code';
  code.style.cssText = 'overflow-x:auto;white-space:pre';
  code.textContent = 'const example = ' + 'long_identifier_'.repeat(90) + ';';
  const rail = document.createElement('div');
  rail.id = 'horizontal-content';
  rail.style.cssText = 'overflow-x:auto;white-space:nowrap';
  const track = document.createElement('div');
  track.style.width = '1400px';
  track.textContent = 'Intentional horizontal content';
  rail.append(track);
  cards[0].append(code, rail);
  window.cardGeometry = () => {
    const width = document.documentElement.clientWidth;
    const inside = (node, parent) => {
      const a = node.getBoundingClientRect(), b = parent.getBoundingClientRect();
      return a.left >= b.left - 1 && a.right <= b.right + 1;
    };
    const scrolls = node => {
      node.scrollLeft = 50;
      return node.scrollWidth > node.clientWidth && node.scrollLeft > 0;
    };
    return {
      width, cards: cards.length,
      dark: matchMedia('(prefers-color-scheme: dark)').matches,
      pageFits: document.documentElement.scrollWidth <= width,
      cardsFit: cards.every(card => inside(card, host) && card.getBoundingClientRect().right <= width),
      contentsFit: cards.every(card => [...card.querySelectorAll('h3, button, .module-ver, .pill, .module-cmd')]
        .every(node => inside(node, card) && node.scrollWidth <= node.clientWidth + 1)),
      buttonsNatural: cards.every(card => card.querySelector('button').getBoundingClientRect().width
        < card.getBoundingClientRect().width - 40),
      codeScrolls: scrolls(code), railScrolls: scrolls(rail),
      codeWhitespace: getComputedStyle(code).whiteSpace,
      commandWhitespace: getComputedStyle(cards[0].querySelector('.module-cmd')).whiteSpace,
      noCardClipping: cards.every(card => getComputedStyle(card).overflowX === 'visible'),
      desktopCap: host.querySelector('.modules').getBoundingClientRect().width <= 820,
    };
  };
  window.moduleCardsReady = true;
})().catch(error => { window.moduleCardsError = String(error); });
