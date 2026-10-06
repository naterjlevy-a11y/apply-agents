"""JavaScript run inside each page/frame. Kept here so browser.py stays readable."""

# Finds every question on the page, tags its element(s) with data-aa ids, and
# returns plain descriptions. Runs per frame; PREFIX keeps ids unique across frames.
SCAN = r"""
(prefix) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    if (!el) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    return el.getClientRects().length > 0;
  };
  const byId = id => id ? document.getElementById(id) : null;
  const ownText = (node, skip) => {
    // text of node without the text of any option labels inside it
    const c = node.cloneNode(true);
    c.querySelectorAll('input,select,textarea,button,ul,[role=listbox],[role=radiogroup] label,[role=option]').forEach(n => n.remove());
    (skip || []).forEach(t => { if (t) c.innerHTML = c.innerHTML.split(t).join(' '); });
    return clean(c.innerText || c.textContent);
  };
  const labelFor = el => {
    let t = '';
    const lb = el.getAttribute('aria-labelledby');
    if (lb) t = lb.split(/\s+/).map(id => clean(byId(id)?.innerText)).join(' ');
    if (!t && el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) t = ownText(l);
    }
    if (!t) { const l = el.closest('label'); if (l) t = ownText(l); }
    if (!t) t = el.getAttribute('aria-label') || '';
    if (!t) {
      let c = el.parentElement;
      for (let i = 0; i < 5 && c && !t; i++, c = c.parentElement) {
        const l = c.querySelector('label, legend, [class*="label" i], [class*="question" i]');
        if (l && !l.contains(el)) t = ownText(l);
      }
    }
    if (!t) t = el.placeholder || el.name || el.id || '';
    return clean(t).slice(0, 500);
  };
  const required = (el, label) => !!(el.required || el.getAttribute('aria-required') === 'true'
                   || /\*\s*$|\*\s*\(|^\*/.test(label) || /\brequired\b/i.test(label));
  const groupQuestion = (inputs) => {
    const first = inputs[0];
    const fs = first.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) return clean(lg.innerText); }
    const rg = first.closest('[role=radiogroup],[role=group]');
    if (rg) {
      const lb = rg.getAttribute('aria-labelledby');
      if (lb) return lb.split(/\s+/).map(id => clean(byId(id)?.innerText)).join(' ');
      if (rg.getAttribute('aria-label')) return clean(rg.getAttribute('aria-label'));
    }
    // climb until the container holds the whole group, then take its own text
    let c = first.parentElement;
    while (c && !inputs.every(i => c.contains(i))) c = c.parentElement;
    const optLabels = inputs.map(labelFor);
    for (let i = 0; i < 4 && c; i++, c = c.parentElement) {
      const t = ownText(c, optLabels);
      if (t.length > 2) return t.slice(0, 500);
    }
    return first.name || '';
  };

  const out = [];
  let n = 0;
  const tag = (el, id) => el.setAttribute('data-aa', id);
  const selOf = (el, id) => el.id && document.querySelectorAll(`[id="${CSS.escape(el.id)}"]`).length === 1
                            ? `[id="${CSS.escape(el.id)}"]` : `[data-aa="${id}"]`;
  const seenGroups = new Set();
  const els = document.querySelectorAll('input, textarea, select');
  for (const el of els) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search', 'password'].includes(type)) continue;
    if (el.disabled || el.readOnly && type !== 'file') continue;
    if (type !== 'file' && type !== 'radio' && type !== 'checkbox' && !visible(el)) continue;
    if (el.closest('[aria-hidden="true"]') && type !== 'file') continue;

    if (type === 'radio' || type === 'checkbox') {
      const name = el.name;
      const group = name ? [...document.querySelectorAll(`input[type="${type}"][name="${CSS.escape(name)}"]`)] : [el];
      const shown = group.filter(g => visible(g) || visible(g.closest('label')) || visible(g.parentElement));
      if (!shown.length) continue;
      if (name && seenGroups.has(type + name)) continue;
      if (name) seenGroups.add(type + name);
      const id = `${prefix}f${n++}`;
      if (type === 'checkbox' && shown.length === 1) {
        tag(el, id);
        const label = labelFor(el);
        out.push({id, sel: selOf(el, id), kind: 'checkbox', label, required: required(el, label), options: [],
                  value: el.checked ? 'checked' : ''});
        continue;
      }
      const options = [];
      shown.forEach((g, k) => { tag(g, `${id}o${k}`); options.push(labelFor(g)); });
      const label = groupQuestion(shown);
      const checked = shown.map((g, k) => g.checked ? options[k] : null).filter(Boolean);
      out.push({id, sel: `[data-aa="${id}o0"]`, kind: type === 'radio' ? 'radio' : 'checkboxes', label, options,
                required: shown.some(g => g.required) || required(el, label), value: checked.join('; ')});
      continue;
    }

    const id = `${prefix}f${n++}`;
    tag(el, id);
    const label = labelFor(el);
    if (el.tagName === 'SELECT') {
      const options = [...el.options].map(o => clean(o.text)).filter(t => t && !/^(select|choose|--|please select)/i.test(t));
      const cur = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : '';
      out.push({id, sel: selOf(el, id), kind: 'select', label, required: required(el, label), options,
                value: options.includes(cur) ? cur : ''});
      continue;
    }
    let kind = el.tagName === 'TEXTAREA' ? 'textarea' : type;
    if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list') kind = 'combobox';
    let value = type === 'file' ? [...(el.files || [])].map(f => f.name).join('; ') : el.value;
    if (kind === 'combobox') {
      // react-select style widgets show the chosen value in a sibling, not in the input
      let c = el.parentElement;
      for (let i = 0; i < 4 && c; i++, c = c.parentElement) {
        const sv = c.querySelector('[class*="singleValue" i], [class*="single-value" i], [class*="multiValue" i]');
        if (sv) { value = clean(sv.innerText); break; }
      }
    }
    if (type === 'file') {
      // the visible text near a hidden upload input says what it is for
      let c = el.parentElement, t = label;
      for (let i = 0; i < 5 && c && (!t || /^(file|upload|attach)/i.test(t) || t === el.name); i++, c = c.parentElement) {
        const own = ownText(c); if (own) t = own;
      }
      out.push({id, sel: selOf(el, id), kind: 'file', label: clean(t).slice(0, 300), required: required(el, label), options: [],
                value, accept: el.accept || ''});
      continue;
    }
    out.push({id, sel: selOf(el, id), kind, label, required: required(el, label), options: [], value: value || '',
              maxlength: el.maxLength > 0 ? el.maxLength : null});
  }

  // Yes/No answered by buttons (Ashby and similar): treat each button row as a radio question.
  const btns = [...document.querySelectorAll('button[type=button], [role=button]')].filter(b => visible(b) && /^(yes|no)$/i.test(clean(b.innerText)));
  const parents = new Set(btns.map(b => b.parentElement));
  for (const p of parents) {
    const row = [...p.children].filter(b => btns.includes(b));
    if (row.length < 2) continue;
    const id = `${prefix}f${n++}`;
    row.forEach((b, k) => b.setAttribute('data-aa', `${id}o${k}`));
    let c = p.parentElement, label = '';
    for (let i = 0; i < 4 && c && !label; i++, c = c.parentElement) label = ownText(c, row.map(b => clean(b.innerText)));
    const pressed = row.filter(b => b.getAttribute('aria-pressed') === 'true' || /active|selected|checked/i.test(b.className));
    out.push({id, sel: `[data-aa="${id}o0"]`, kind: 'buttons', label: label.slice(0, 500), required: /\*/.test(label), options: row.map(b => clean(b.innerText)),
              value: pressed.map(b => clean(b.innerText)).join('; ')});
  }
  return out;
}
"""

# Reads back current values for given ids (same shapes as SCAN's `value`).
READ = r"""
(pairs) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = {};
  for (const [id, sel, fileName] of pairs) {
    const el = document.querySelector(sel);
    const opts = [...document.querySelectorAll(`[data-aa^="${id}o"]`)];
    if (opts.length) {
      out[id] = opts.filter(o => o.checked || o.getAttribute('aria-pressed') === 'true'
                 || (o.tagName === 'BUTTON' && /active|selected|checked/i.test(o.className)))
                 .map(o => o.tagName === 'BUTTON' ? clean(o.innerText) : (o.labels && o.labels[0] ? clean(o.labels[0].innerText) : o.value)).join('; ');
      continue;
    }
    if (!el) { out[id] = fileName && (document.body.innerText || '').includes(fileName) ? fileName : null; continue; }
    if (el.tagName === 'SELECT') out[id] = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : '';
    else if (el.type === 'checkbox') out[id] = el.checked ? 'checked' : '';
    else if (el.type === 'file') {
      // upload widgets often move the file elsewhere and show its name instead
      let v = [...(el.files || [])].map(f => f.name).join('; '), c = el.parentElement;
      for (let i = 0; i < 6 && c && !v; i++, c = c.parentElement) if (fileName && (c.innerText || '').includes(fileName)) v = fileName;
      if (!v && fileName && (document.body.innerText || '').includes(fileName)) v = fileName;
      out[id] = v;
    }
    else if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list') {
      let v = el.value, c = el.parentElement;
      for (let i = 0; i < 4 && c; i++, c = c.parentElement) {
        const sv = c.querySelector('[class*="singleValue" i], [class*="single-value" i], [class*="multiValue" i]');
        if (sv) { v = clean(sv.innerText); break; }
      }
      out[id] = v;
    } else out[id] = el.value;
  }
  return out;
}
"""

# Page-level facts: buttons that move forward, blockers, and a title for company/role.
PAGE_STATE = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  document.querySelectorAll('[data-aa-btn]').forEach(b => b.removeAttribute('data-aa-btn'));
  const buttons = [...document.querySelectorAll('button, input[type=submit], [role=button], a')].filter(visible);
  const text = b => clean(b.innerText || b.value || b.getAttribute('aria-label'));
  const find = (re, kinds) => buttons.filter(b => re.test(text(b)) && (!kinds || kinds.includes(b.tagName)));
  const mark = (list, name) => list.length ? (list[list.length - 1].setAttribute('data-aa-btn', name), text(list[list.length - 1])) : '';
  const submitList = find(/^(submit( application| my application)?|send application|apply|complete application|finish)$/i, ['BUTTON', 'INPUT']);
  const nextList = find(/^(next|continue|save (and|&) continue|next step|proceed)$/i, ['BUTTON', 'INPUT', 'A']);
  const applyList = find(/^(apply( now| for this (job|position|role)| here| online)?|i'?m interested|start (your )?application|application)$/i);
  const iframes = [...document.querySelectorAll('iframe')].filter(visible).map(f => f.src || '');
  const body = clean(document.body ? document.body.innerText : '').slice(0, 4000);
  const meta = n => document.querySelector(`meta[property="${n}"], meta[name="${n}"]`)?.content || '';
  return {
    title: document.title, og_title: meta('og:title'), og_site: meta('og:site_name'),
    h1: clean(document.querySelector('h1')?.innerText).slice(0, 200),
    submit: mark(submitList, 'submit'), next: mark(nextList, 'next'), apply: mark(applyList, 'apply'),
    apply_href: (applyList.length && applyList[applyList.length - 1].href) || '',
    password: [...document.querySelectorAll('input[type=password]')].some(visible),
    captcha_challenge: iframes.some(s => (/recaptcha\/(api2|enterprise)\/anchor/.test(s) && !/size=invisible/.test(s))
                       || /hcaptcha\.com.*checkbox|challenges\.cloudflare\.com/.test(s)),
    captcha_invisible: iframes.some(s => /size=invisible/.test(s)) || !!document.querySelector('.grecaptcha-badge'),
    interstitial: /just a moment|verify you are human|checking your browser|are you a robot/i.test(document.title + ' ' + body.slice(0, 300)),
    closed: /no longer (accepting|available|open)|position has been filled|job (is )?not found|this job (posting )?(has )?(expired|closed)|page (you('| a)re looking for )?(was )?not found/i.test(body.slice(0, 1500)),
    confirmation: /thank(s| you) for (applying|your (application|interest))|application (has been |was )?(received|submitted)|we('ve| have) received your application/i.test(body),
    body: body.slice(0, 1500),
  };
}
"""

# Options of an open combobox: the listbox it controls, else the visible listbox nearest below it.
COMBO_OPTIONS = r"""
(sel) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const el = document.querySelector(sel);
  if (!el) return [];
  const ctl = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
  let box = ctl ? document.getElementById(ctl) : null;
  if (!box) {
    // menus open below the field, or above it near the bottom of the screen
    const r = el.getBoundingClientRect();
    const gap = b => { const q = b.getBoundingClientRect(); return Math.min(Math.abs(q.top - r.bottom), Math.abs(q.bottom - r.top)); };
    const boxes = [...document.querySelectorAll('[role=listbox]')].filter(b => b.getClientRects().length);
    boxes.sort((a, b) => gap(a) - gap(b));
    box = boxes[0];
    if (box && gap(box) > 200) box = null;
  }
  if (!box) return [];
  const opts = [...box.querySelectorAll('[role=option]')];
  opts.forEach((o, k) => o.setAttribute('data-aa-opt', String(k)));
  document.querySelectorAll('[data-aa-opt]').forEach(o => { if (!box.contains(o)) o.removeAttribute('data-aa-opt'); });
  return opts.map(o => clean(o.innerText));
}
"""
