'use strict';

// Connections express topic membership, not prerequisite ordering.
function renderProblemGraph(available, search) {
  const graph = $('problem-list'), category = $('category').value;
  const overview = !category && !search;
  const items = overview
    ? [...new Set(available.map(p => p.category))].map(topic => ({topic, count:available.filter(p => p.category === topic).length}))
    : available.filter(p => `${p.title} ${p.number}`.toLowerCase().includes(search));
  $('graph-back').hidden = !category && !search;
  graph.replaceChildren(); graph.scrollTop = 0;
  if (!items.length) { graph.append(element('p', 'No problems', 'graph-empty')); return; }

  const rowHeight = 88, start = 78, height = start + Math.ceil(items.length / 2) * rowHeight;
  const canvas = element('div', undefined, 'graph-canvas'); canvas.style.height = `${height}px`;
  const root = element('span', overview ? 'Topics' : category || 'Results', 'graph-root');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 600 ${height}`); svg.setAttribute('preserveAspectRatio', 'none'); svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(svg.namespaceURI, 'path');
  const last = start + Math.floor((items.length - 1) / 2) * rowHeight + 36;
  let connections = `M300 40 V${last}`;

  items.forEach((item, index) => {
    const y = start + Math.floor(index / 2) * rowHeight;
    const node = element('button', undefined, `graph-node ${overview ? 'topic-node' : `difficulty-${item.difficulty}`}`);
    node.type = 'button'; node.style.top = `${y}px`; node.style.setProperty('--node-delay', `${Math.min(index, 8) * 18}ms`);
    node.classList.add(index % 2 ? 'right' : 'left');
    connections += ` M300 ${y + 36} H${index % 2 ? 330 : 270}`;
    if (overview) {
      node.dataset.category = item.topic;
      const picked = catalog.filter(p => p.category === item.topic && selected.has(p.id)).length;
      node.classList.toggle('has-selection', picked > 0);
      node.append(element('strong', item.topic), element('small', `${item.count} problems${picked ? ` · ${picked} selected` : ''}`));
      node.setAttribute('aria-label', `${item.topic}, ${item.count} problems`);
      node.onclick = () => {
        $('category').value = item.topic; renderPicker();
        graph.querySelector('button')?.focus({preventScroll:true});
      };
    } else {
      node.dataset.problem = item.id;
      node.append(element('strong', item.title), element('small', item.difficulty));
      node.classList.toggle('selected', selected.has(item.id));
      node.setAttribute('aria-pressed', String(selected.has(item.id)));
      node.onclick = () => {
        if (selected.has(item.id)) selected.delete(item.id);
        else {
          if (selected.size >= 5) { notice('Choose up to 5 problems.'); return; }
          selected.add(item.id);
        }
        node.classList.toggle('selected', selected.has(item.id));
        node.setAttribute('aria-pressed', String(selected.has(item.id)));
        if (selected.size) $('problem-count').value = selected.size;
        updateMatching(); notice();
      };
    }
    canvas.append(node);
  });
  path.setAttribute('d', connections); path.setAttribute('vector-effect', 'non-scaling-stroke'); svg.append(path);
  canvas.prepend(svg, root); graph.append(canvas);
}
