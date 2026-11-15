/*
 * Kagger.AI - Deep Research report renderer ("AI Advanced Search").
 *
 * Turns a Parallel Task API output ({ type, content, basis }) into:
 *   1. a styled on-screen research report (masthead, numbered sections, styled
 *      tables, callouts, superscript footnotes, sources list, disclosures), and
 *   2. a natively-typeset, text-selectable PDF (cover page, contents, running
 *      headers/footers, page numbers, bookmarks, linked footnotes and sources).
 *
 * Parallel embeds citation markers in field text as "[field[i]] [N]": `field[i]`
 * indexes output.basis[field].citations, and N is the engine's global source id.
 * Markers are resolved against basis and renumbered in order of first use.
 *
 * Exposes window.KaggerReport = { build, render, downloadPdf, compactOutput, deriveTitle }.
 */
(function (global) {
  'use strict';

  var DEPTH_LABELS = {
    'pro-fast': 'Deep Research',
    'ultra-fast': 'Advanced Deep Research',
    'ultra': 'Super Advanced Deep Research'
  };

  var META_KEYS = ['title', 'run_id', 'status', 'created_at', 'completed_at', 'processor', 'warnings',
    'error', 'taskgroup_id', 'basis', 'sources'];
  var LEAD_KEYS = ['executive_summary', 'summary', 'overview', 'key_findings', 'tldr'];

  var SMALL_WORDS = ['a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'from', 'in', 'into', 'of', 'on', 'or',
    'over', 'the', 'to', 'vs', 'versus', 'with', 'via', 'per', 'than', 'nor'];
  var ACRONYMS = ['ai', 'it', 'us', 'uk', 'eu', 'uae', 'gdp', 'ev', 'evs', 'esg', 'ipo', 'ipos', 'fmcg', 'nbfc',
    'nbfcs', 'rbi', 'sebi', 'nse', 'bse', 'pli', 'ebitda', 'pat', 'roe', 'roce', 'eps', 'fii', 'fiis', 'dii', 'diis',
    'etf', 'psu', 'psus', 'bfsi', 'it/ites', 'cdmo', 'api', 'apis', 'ems', 'saas', 'gst', 'capex', 'opex', 'aum',
    'nim', 'nims', 'npa', 'npas', 'emi', 'ihcl', 'l&t', 'sbi', 'hdfc', 'icici', 'bhel', 'ntpc', 'ongc', 'lic', 'tcs',
    'srf', 'yoy', 'qoq', 'cagr', 'mhe', 'r&d', 'pe', 'p/e', 'ev/ebitda', 'kpi', 'kpis', 'ceo', 'cfo', 'md', 'ppa'];
  var CAPEX_STYLE = { capex: 'Capex', opex: 'Opex' };

  // ------------------------------------------------------------------ utils
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function hostOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, ''); } catch (e) { return ''; }
  }

  function safeUrl(url) {
    return /^https?:\/\//i.test(String(url || '')) ? String(url) : '';
  }

  function smartTitle(raw) {
    var t = String(raw || '').replace(/_/g, ' ').replace(/\s+/g, ' ').trim();
    t = t.replace(/^(?:section\s+)?\d+(?:\.\d+)*[\s.):\-]+(?=\S)/i, '');
    var words = t.split(' ');
    return words.map(function (w, i) {
      var lower = w.toLowerCase();
      var bare = lower.replace(/[^a-z0-9&/]/g, '');
      if (CAPEX_STYLE[bare]) return w.replace(new RegExp(bare, 'i'), CAPEX_STYLE[bare]);
      if (ACRONYMS.indexOf(bare) !== -1) return w.toUpperCase();
      if (/^(fy|q|h|cy)\d/i.test(bare) || /^\d+(h|q)(fy|cy)?\d*$/i.test(bare)) return w.toUpperCase();
      if (i > 0 && i < words.length - 1 && SMALL_WORDS.indexOf(lower) !== -1) return lower;
      if (/[A-Z].*[A-Z]/.test(w.slice(1))) return w;       // keep mixed-case brand names
      return w.charAt(0).toUpperCase() + w.slice(1);
    }).join(' ');
  }

  function deriveTitle(question) {
    var q = String(question || '').replace(/\s+/g, ' ').trim();
    if (!q) return 'Deep Research Report';
    q = q.replace(/^(?:please\s+)?(?:give me|get me|provide(?: me)?|show me|find(?: me)?|list(?: out)?|tell me(?: about)?|i need|i want|can you(?: give me| find| list)?|could you(?: give me| find| list)?|help me (?:find|understand)|do (?:a|an) |prepare (?:a|an) )\s*/i, '');
    q = q.replace(/^(?:a|an|the)\s+/i, '');
    var first = q.split(/(?<=[.?!])\s/)[0].replace(/[.?!]+$/, '');
    if (first.length > 96) {
      first = first.slice(0, 96);
      first = first.slice(0, first.lastIndexOf(' ')).replace(/[,;:\-+&]+$/, '').trim() + '…';
    }
    return first.charAt(0).toUpperCase() + first.slice(1);
  }

  function formatDate(d) {
    try {
      return new Date(d).toLocaleDateString('en-IN', { day: 'numeric', month: 'long', year: 'numeric' });
    } catch (e) { return String(d); }
  }

  // ------------------------------------------------------- content parsing
  function parseMaybeJson(v) {
    if (typeof v !== 'string') return v;
    var t = v.trim();
    if (t.charAt(0) === '{' || t.charAt(0) === '[') {
      try { return JSON.parse(t); } catch (e) { /* not JSON */ }
    }
    return v;
  }

  function cellText(v) {
    if (v == null) return '';
    if (Array.isArray(v)) return v.map(cellText).join('; ');
    if (typeof v === 'object') {
      return Object.keys(v).map(function (k) { return smartTitle(k) + ': ' + cellText(v[k]); }).join('; ');
    }
    return String(v).replace(/\|/g, '\\|').replace(/\r?\n+/g, '<br>');
  }

  function valueToMarkdown(value, depth) {
    if (value == null) return '';
    if (typeof value !== 'object') return String(value) + '\n\n';
    if (Array.isArray(value)) {
      if (!value.length) return '';
      var allObjects = value.every(function (x) { return x && typeof x === 'object' && !Array.isArray(x); });
      if (allObjects) {
        var keys = [];
        value.forEach(function (o) { Object.keys(o).forEach(function (k) { if (keys.indexOf(k) === -1) keys.push(k); }); });
        var shortish = value.every(function (o) {
          return keys.every(function (k) { return o[k] == null || typeof o[k] !== 'object' || Array.isArray(o[k]); });
        });
        if (keys.length <= 7 && shortish) {
          var md = '| ' + keys.map(smartTitle).join(' | ') + ' |\n|' + keys.map(function () { return '---'; }).join('|') + '|\n';
          value.forEach(function (o) {
            md += '| ' + keys.map(function (k, i) {
              var c = cellText(o[k]);
              return i === 0 && c ? '**' + c + '**' : c;
            }).join(' | ') + ' |\n';
          });
          return md + '\n';
        }
        return value.map(function (o) {
          var name = o.company_name || o.name || o.title || '';
          var out = name ? '#'.repeat(Math.min(depth + 3, 4)) + ' ' + name + '\n\n' : '';
          Object.keys(o).forEach(function (k) {
            if (['company_name', 'name', 'title'].indexOf(k) !== -1) return;
            var v = o[k];
            if (v && typeof v === 'object') out += '**' + smartTitle(k) + '**\n\n' + valueToMarkdown(v, depth + 1);
            else out += '- **' + smartTitle(k) + ':** ' + cellText(v) + '\n';
          });
          return out + '\n';
        }).join('');
      }
      return value.map(function (x) { return '- ' + cellText(x) + '\n'; }).join('') + '\n';
    }
    var md2 = '';
    Object.keys(value).forEach(function (k) {
      var v = value[k];
      if (v && typeof v === 'object') md2 += '#'.repeat(Math.min(depth + 3, 4)) + ' ' + smartTitle(k) + '\n\n' + valueToMarkdown(v, depth + 1);
      else md2 += '- **' + smartTitle(k) + ':** ' + cellText(v) + '\n';
    });
    return md2 + '\n';
  }

  function contentToMarkdown(content) {
    var data = parseMaybeJson(content);
    if (data && typeof data === 'object' && !Array.isArray(data) && typeof data.answer === 'string' && Object.keys(data).length <= 3) {
      data = parseMaybeJson(data.answer);
    }
    if (typeof data === 'string') return { markdown: data, title: '' };
    if (Array.isArray(data)) return { markdown: valueToMarkdown(data, 0), title: '' };

    var keys = Object.keys(data).filter(function (k) { return META_KEYS.indexOf(k) === -1; });
    var lead = keys.filter(function (k) { return LEAD_KEYS.indexOf(k.toLowerCase()) !== -1; });
    var rest = keys.filter(function (k) { return lead.indexOf(k) === -1; });
    var md = '';
    lead.concat(rest).forEach(function (k) {
      var v = parseMaybeJson(data[k]);
      if (v == null || v === '') return;
      md += '## ' + smartTitle(k) + '\n\n' + valueToMarkdown(v, 0);
    });
    return { markdown: md, title: typeof data.title === 'string' ? data.title : '' };
  }

  // -------------------------------------------------------------- citations
  var FIELD_MARK = '\\[([A-Za-z0-9_.\\-]+)\\[(\\d+)\\]\\](?:[ \\t]*\\[(\\d+)\\])?';
  var NUM_MARK = '\\[(\\d{1,3})\\](?![(:\\[])';
  var ONE_MARK = new RegExp(FIELD_MARK + '|' + NUM_MARK, 'g');
  var MARK_RUN = new RegExp('(?:[ \\t]*(?:' + FIELD_MARK + '|' + NUM_MARK + '))+', 'g');

  function indexBasis(basis) {
    var map = {};
    (Array.isArray(basis) ? basis : []).forEach(function (b) {
      if (b && b.field) map[b.field] = b;
    });
    return map;
  }

  function processCitations(md, basis) {
    var basisMap = indexBasis(basis);
    var hasBasis = Object.keys(basisMap).length > 0;
    var ctx = { sources: [], byUrl: {}, byN: {}, byField: {} };
    // Renumber only when field markers can be resolved against basis; otherwise keep the
    // engine's own numbers so they still line up with any reference list inside the text.
    var renumber = hasBasis && new RegExp(FIELD_MARK).test(md);

    function resolve(field, idx, n) {
      var cit = null;
      if (field && hasBasis) {
        var b = basisMap[field];
        cit = (b && b.citations && b.citations[idx]) ||
          (basisMap[field + '.' + idx] && (basisMap[field + '.' + idx].citations || [])[0]) || null;
      }
      var url = cit && safeUrl(cit.url);
      if (!renumber) {
        if (!n) return null;
        return { num: Number(n), linked: false };
      }
      var num = (url && ctx.byUrl[url]) || (n && ctx.byN[n]) || (!n && !url && ctx.byField[field + '#' + idx]);
      if (!num) {
        num = ctx.sources.length + 1;
        ctx.sources.push({
          num: num,
          url: url || '',
          title: (cit && cit.title) || '',
          excerpt: cit && Array.isArray(cit.excerpts) ? (cit.excerpts.filter(Boolean)[0] || '') : '',
          ref: n || ''
        });
      } else if (url && !ctx.sources[num - 1].url) {
        ctx.sources[num - 1].url = url;
        ctx.sources[num - 1].title = ctx.sources[num - 1].title || (cit && cit.title) || '';
      }
      if (url) ctx.byUrl[url] = num;
      if (n) ctx.byN[n] = num;
      if (!n && !url) ctx.byField[field + '#' + idx] = num;
      return { num: num, linked: true };
    }

    var out = md.replace(MARK_RUN, function (run) {
      var nums = [];
      var linked = false;
      run.replace(ONE_MARK, function (m, field, idx, n1, n2) {
        var r = field ? resolve(field, Number(idx), n1) : resolve(null, 0, n2);
        if (r && nums.indexOf(r.num) === -1) { nums.push(r.num); linked = linked || r.linked; }
        return m;
      });
      if (!nums.length) return '';
      nums.sort(function (a, b) { return a - b; });
      var inner = nums.map(function (n) {
        var s = ctx.sources[n - 1];
        var tip = s ? esc((s.title || hostOf(s.url) || 'Source ' + n) + (s.url ? ' — ' + hostOf(s.url) : '')) : 'Source ' + n;
        return linked
          ? '<a href="#kr-src-' + n + '" data-fn="' + n + '" title="' + tip + '">' + n + '</a>'
          : '<span data-fn="' + n + '" title="Source ' + n + '">' + n + '</span>';
      }).join('<span class="kr-fn-sep">,</span>');
      return '<sup class="kr-fn">' + inner + '</sup>';
    });

    var consulted = [];
    if (!ctx.sources.length && hasBasis) {
      var seen = {};
      Object.keys(basisMap).forEach(function (f) {
        (basisMap[f].citations || []).forEach(function (c) {
          var u = safeUrl(c && c.url);
          if (u && !seen[u]) { seen[u] = 1; consulted.push({ url: u, title: c.title || '', excerpt: '' }); }
        });
      });
    }
    return { markdown: out, sources: ctx.sources, consulted: consulted };
  }

  // -------------------------------------------------------------- build DOM
  function typographic(md) {
    return md
      .replace(/(\s)->(\s)/g, '$1→$2')
      .replace(/(\s)=>(\s)/g, '$1⇒$2')
      .replace(/(\d)\s?-\s?(\d+)\s?bps\b/g, '$1–$2 bps');
  }

  var TAKEAWAY_RE = /^(key\s+)?(takeaway|takeaways|bottom line|conclusion|verdict|implication|implications|what it means|investor takeaway)s?\s*:?$/i;
  var NOTE_RE = /^(scope|note|notes|methodology|caveat|caveats|important|disclaimer|assumptions?|limitations?|data note)\s*:?$/i;

  function sanitize(html) {
    if (global.DOMPurify) {
      return global.DOMPurify.sanitize(html, { ADD_ATTR: ['target', 'data-fn'], FORBID_TAGS: ['style', 'form', 'input'] });
    }
    return html;
  }

  function enhanceBody(body) {
    // Group into sections at each h2.
    var nodes = Array.prototype.slice.call(body.childNodes);
    var frag = document.createDocumentFragment();
    var section = null;
    nodes.forEach(function (n) {
      if (n.nodeType === 1 && n.tagName === 'H2') {
        section = document.createElement('section');
        section.className = 'kr-section';
        frag.appendChild(section);
      } else if (!section) {
        section = document.createElement('section');
        section.className = 'kr-section kr-preamble';
        frag.appendChild(section);
      }
      section.appendChild(n);
    });
    body.innerHTML = '';
    body.appendChild(frag);

    // Demote any h1 coming from the content (the masthead owns the h1).
    body.querySelectorAll('h1').forEach(function (h) {
      var h2 = document.createElement('h2');
      h2.innerHTML = h.innerHTML;
      h.replaceWith(h2);
    });

    var secNum = 0;
    body.querySelectorAll('section.kr-section').forEach(function (sec, i) {
      var h = sec.querySelector('h2');
      if (!h) return;
      var title = smartTitle(h.textContent);
      var isExec = /^(executive summary|summary|overview|key findings|tl;?dr)$/i.test(title);
      if (isExec) sec.classList.add('kr-exec');
      h.id = 'kr-sec-' + i;
      h.setAttribute('data-title', title);
      if (isExec) {
        h.innerHTML = '<span class="kr-sec-title">' + esc(title) + '</span>';
      } else {
        secNum += 1;
        h.setAttribute('data-num', secNum);
        h.innerHTML = '<span class="kr-sec-num">' + secNum + '</span><span class="kr-sec-title">' + esc(title) + '</span>';
      }
    });

    body.querySelectorAll('table').forEach(function (t) {
      var wrap = document.createElement('div');
      wrap.className = 'kr-table-wrap';
      t.parentNode.insertBefore(wrap, t);
      wrap.appendChild(t);
      t.classList.add('kr-table');
      var cols = (t.rows[0] && t.rows[0].cells.length) || 0;
      if (cols >= 5) t.classList.add('kr-table-dense');
    });

    body.querySelectorAll('p').forEach(function (p) {
      var first = p.firstElementChild;
      if (!first || first !== p.firstChild && (p.firstChild.nodeType !== 3 || p.firstChild.textContent.trim())) return;
      if (!/^(STRONG|B)$/.test(first.tagName)) return;
      var label = first.textContent.trim();
      var kind = TAKEAWAY_RE.test(label) ? 'takeaway' : NOTE_RE.test(label) ? 'note' : '';
      if (!kind) return;
      var box = document.createElement('div');
      box.className = 'kr-callout kr-callout-' + kind;
      var lab = document.createElement('div');
      lab.className = 'kr-callout-label';
      lab.textContent = label.replace(/:\s*$/, '');
      first.remove();
      p.innerHTML = p.innerHTML.replace(/^\s*:?\s*/, '');
      p.parentNode.insertBefore(box, p);
      box.appendChild(lab);
      box.appendChild(p);
    });

    body.querySelectorAll('blockquote').forEach(function (bq) {
      var m = bq.textContent.trim().match(/^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]/i);
      bq.classList.add('kr-quote');
      if (m) {
        bq.innerHTML = bq.innerHTML.replace(/\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*/i, '');
        bq.className = 'kr-callout kr-callout-note';
      }
    });

    body.querySelectorAll('a[href]').forEach(function (a) {
      if (a.getAttribute('href').charAt(0) === '#') return;
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
    });
  }

  /**
   * Build the report model + DOM.
   * opts: { output, legacyMarkdown, question, depth, generatedAt, title }
   */
  function build(opts) {
    var output = opts.output || null;
    var md = '', title = opts.title || '', basis = [];
    if (output) {
      var c = contentToMarkdown(output.content != null ? output.content : output);
      md = c.markdown;
      title = title || c.title;
      basis = output.basis || [];
    } else {
      md = opts.legacyMarkdown || '';
      // Legacy markdown carried "# Research Summary"-style h1s; masthead replaces them.
    }
    var cit = processCitations(typographic(md), basis);
    var parse = (global.marked && (global.marked.parse || global.marked)) || function (s) { return s; };
    var html = sanitize(parse.call(global.marked, cit.markdown));

    var body = document.createElement('div');
    body.className = 'kr-body';
    body.innerHTML = html;
    enhanceBody(body);

    var words = (body.textContent || '').split(/\s+/).filter(Boolean).length;
    var headings = Array.prototype.map.call(body.querySelectorAll('h2'), function (h) { return h.getAttribute('data-title') || h.textContent; });

    return {
      title: title || deriveTitle(opts.question),
      titleIsFinal: !!title,
      question: opts.question || '',
      depth: opts.depth || '',
      depthLabel: DEPTH_LABELS[opts.depth] || (opts.depth ? smartTitle(opts.depth) : 'Deep Research'),
      generatedAt: opts.generatedAt || new Date().toISOString(),
      sources: cit.sources,
      consulted: cit.consulted,
      legacy: !output,
      readMinutes: Math.max(1, Math.round(words / 220)),
      headings: headings,
      body: body
    };
  }

  var DISCLAIMER = 'This report was generated by Kagger.AI’s autonomous research engine from publicly available ' +
    'information such as company disclosures, earnings-call commentary and media coverage. It is provided for ' +
    'information and research purposes only and does not constitute investment advice, a research recommendation, ' +
    'or an offer or solicitation to buy or sell any security. Management guidance is reported as disclosed and may be ' +
    'revised or withdrawn; figures may contain errors or omissions. Verify against primary filings and consult a ' +
    'SEBI-registered investment adviser before making investment decisions.';

  function sourcesHtml(model) {
    if (model.sources.length) {
      return '<ol class="kr-sources-list">' + model.sources.map(function (s) {
        var host = hostOf(s.url);
        var title = s.title || host || ('Source ' + s.num);
        return '<li id="kr-src-' + s.num + '"><span class="kr-src-num">' + s.num + '</span><div class="kr-src-body">' +
          '<div class="kr-src-title">' + (s.url ? '<a href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer">' + esc(title) + '</a>' : esc(title)) + '</div>' +
          (host ? '<div class="kr-src-host">' + esc(host) + '</div>' : '') +
          (s.excerpt ? '<blockquote class="kr-src-excerpt">' + esc(s.excerpt.length > 280 ? s.excerpt.slice(0, 277) + '…' : s.excerpt) + '</blockquote>' : '') +
          '</div></li>';
      }).join('') + '</ol>';
    }
    if (model.consulted.length) {
      return '<p class="kr-src-intro">Sources consulted by the research engine:</p><ul class="kr-sources-list kr-consulted">' +
        model.consulted.map(function (s) {
          return '<li><div class="kr-src-body"><div class="kr-src-title"><a href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer">' +
            esc(s.title || hostOf(s.url)) + '</a></div><div class="kr-src-host">' + esc(hostOf(s.url)) + '</div></div></li>';
        }).join('') + '</ul>';
    }
    return '<p class="kr-src-intro">Source links are not available for this report' +
      (model.legacy ? ' (it was saved before source tracking was added; run the query again to get linked sources).' : '.') + '</p>';
  }

  function render(model, container, tocEl) {
    ensureStyles();
    var srcCount = model.sources.length || model.consulted.length;
    var article = document.createElement('article');
    article.className = 'kr-report';
    article.innerHTML =
      '<header class="kr-masthead">' +
        '<div class="kr-brandrow"><span class="kr-brand">Kagger.AI</span><span class="kr-doctype">Deep Research Report</span></div>' +
        '<h1 class="kr-title">' + esc(model.title) + '</h1>' +
        '<dl class="kr-meta">' +
          '<div><dt>Published</dt><dd>' + esc(formatDate(model.generatedAt)) + '</dd></div>' +
          '<div><dt>Research depth</dt><dd>' + esc(model.depthLabel) + '</dd></div>' +
          '<div><dt>Sources cited</dt><dd>' + (srcCount || '—') + '</dd></div>' +
          '<div><dt>Reading time</dt><dd>' + model.readMinutes + ' min</dd></div>' +
        '</dl>' +
        (model.question ? '<div class="kr-question"><div class="kr-label">Research question</div><p>' + esc(model.question) + '</p></div>' : '') +
      '</header>';
    article.appendChild(model.body);
    var src = document.createElement('section');
    src.className = 'kr-section kr-sources';
    src.innerHTML = '<h2 id="kr-sources"><span class="kr-sec-title">Sources &amp; References</span></h2>' + sourcesHtml(model);
    article.appendChild(src);
    var foot = document.createElement('footer');
    foot.className = 'kr-disclaimer';
    foot.innerHTML = '<div class="kr-label">Important information</div><p>' + esc(DISCLAIMER) + '</p>';
    article.appendChild(foot);

    container.innerHTML = '';
    container.appendChild(article);

    if (tocEl) {
      tocEl.innerHTML = '';
      var items = Array.prototype.slice.call(article.querySelectorAll('.kr-body h2, .kr-sources h2'));
      items.forEach(function (h) {
        var a = document.createElement('a');
        a.href = '#' + h.id;
        a.className = 'kr-toc-link';
        var num = h.getAttribute('data-num');
        a.innerHTML = (num ? '<span class="kr-toc-num">' + num + '</span>' : '<span class="kr-toc-num kr-toc-dot"></span>') +
          '<span>' + esc(h.getAttribute('data-title') || h.textContent) + '</span>';
        a.addEventListener('click', function (e) {
          e.preventDefault();
          h.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
        tocEl.appendChild(a);
      });
      if ('IntersectionObserver' in global) {
        var links = tocEl.querySelectorAll('a');
        var io = new IntersectionObserver(function (entries) {
          entries.forEach(function (en) {
            if (!en.isIntersecting) return;
            var idx = items.indexOf(en.target);
            links.forEach(function (l, i) { l.classList.toggle('kr-active', i === idx); });
          });
        }, { rootMargin: '-15% 0px -75% 0px' });
        items.forEach(function (h) { io.observe(h); });
      }
    }
    article.querySelectorAll('sup.kr-fn a').forEach(function (a) {
      a.addEventListener('click', function (e) {
        var t = article.querySelector(a.getAttribute('href'));
        if (!t) return;
        e.preventDefault();
        t.scrollIntoView({ behavior: 'smooth', block: 'center' });
        t.classList.remove('kr-flash'); void t.offsetWidth; t.classList.add('kr-flash');
      });
    });
    return article;
  }

  function setTitle(model, container, title) {
    model.title = title;
    model.titleIsFinal = true;
    var h = container.querySelector('.kr-title');
    if (h) h.textContent = title;
  }

  // ----------------------------------------------------------------- styles
  function ensureStyles() {
    if (document.getElementById('kr-report-styles')) return;
    var css = [
      '.kr-report{--kr-ink:#0f172a;--kr-body:#334155;--kr-muted:#64748b;--kr-faint:#94a3b8;--kr-rule:#e2e8f0;--kr-soft:#f8fafc;--kr-accent:#4f46e5;--kr-accent-soft:#eef2ff;--kr-accent-ink:#3730a3;--kr-amber-soft:#fffbeb;--kr-amber:#d97706;',
      'font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--kr-body);font-size:15.5px;line-height:1.72;-webkit-font-smoothing:antialiased;max-width:none}',
      '.kr-report *{box-sizing:border-box}',
      '.kr-masthead{border-bottom:1px solid var(--kr-rule);padding-bottom:28px;margin-bottom:8px}',
      '.kr-brandrow{display:flex;justify-content:space-between;align-items:center;gap:12px;font-size:11px;letter-spacing:.14em;text-transform:uppercase;font-weight:700;margin-bottom:22px}',
      '.kr-brand{color:var(--kr-accent)}.kr-doctype{color:var(--kr-faint)}',
      '.kr-title{font-family:"Source Serif 4",Georgia,"Times New Roman",serif;font-size:clamp(26px,3.2vw,38px);line-height:1.18;font-weight:700;color:var(--kr-ink);letter-spacing:-.01em;margin:0 0 22px}',
      '.kr-meta{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0;margin:0 0 22px;border-top:1px solid var(--kr-rule);border-bottom:1px solid var(--kr-rule)}',
      '.kr-meta>div{padding:12px 14px 12px 0}.kr-meta>div+div{padding-left:14px;border-left:1px solid var(--kr-rule)}',
      '.kr-meta dt{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--kr-faint);font-weight:600;margin:0}',
      '.kr-meta dd{margin:2px 0 0;font-size:14px;font-weight:600;color:var(--kr-ink)}',
      '.kr-label{font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;font-weight:700;color:var(--kr-accent);margin-bottom:6px}',
      '.kr-question{background:var(--kr-soft);border-radius:12px;padding:16px 18px}',
      '.kr-question p{margin:0;font-size:14.5px;line-height:1.6;color:var(--kr-body);font-style:italic}',
      '.kr-section{padding-top:8px}',
      '.kr-section h2{display:flex;align-items:baseline;gap:14px;font-family:"Source Serif 4",Georgia,serif;font-size:24px;line-height:1.3;font-weight:700;color:var(--kr-ink);margin:44px 0 16px;padding-bottom:12px;border-bottom:2px solid var(--kr-ink);scroll-margin-top:120px}',
      '.kr-sec-num{flex:none;font-family:Inter,system-ui,sans-serif;font-size:13px;font-weight:700;color:#fff;background:var(--kr-accent);border-radius:6px;min-width:28px;height:28px;display:inline-flex;align-items:center;justify-content:center;transform:translateY(-3px)}',
      '.kr-section h3{font-size:17px;font-weight:700;color:var(--kr-ink);margin:30px 0 10px;line-height:1.4}',
      '.kr-section h4{font-size:14px;font-weight:700;color:var(--kr-ink);margin:22px 0 8px;text-transform:uppercase;letter-spacing:.05em}',
      '.kr-section p{margin:0 0 14px}',
      '.kr-section strong{color:var(--kr-ink);font-weight:650}',
      '.kr-section a{color:var(--kr-accent);text-decoration:underline;text-decoration-color:rgba(79,70,229,.35);text-underline-offset:2px}',
      '.kr-section ul,.kr-section ol{margin:0 0 16px;padding-left:22px}',
      '.kr-section ul{list-style:disc}.kr-section ol{list-style:decimal}.kr-section ul ul{list-style:circle;margin:8px 0 0}',
      '.kr-section li{margin:0 0 8px;padding-left:4px}',
      '.kr-section ul>li::marker{color:var(--kr-accent)}',
      '.kr-section ol>li::marker{color:var(--kr-accent);font-weight:700}',
      '.kr-exec{background:linear-gradient(180deg,var(--kr-accent-soft),#fff 85%);border:1px solid #e0e7ff;border-radius:16px;padding:6px 26px 14px;margin-top:28px}',
      '.kr-exec h2{border-bottom-color:var(--kr-accent);margin-top:22px}',
      '.kr-exec>p:first-of-type{font-size:16.5px;line-height:1.7;color:var(--kr-ink)}',
      '.kr-table-wrap{margin:20px 0 22px;overflow-x:auto;border:1px solid var(--kr-rule);border-radius:12px;-webkit-overflow-scrolling:touch}',
      '.kr-table{width:100%;border-collapse:collapse;font-size:13.5px;line-height:1.55;margin:0}',
      '.kr-table thead th{background:var(--kr-ink);color:#fff;text-align:left;font-weight:600;font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;padding:11px 14px;vertical-align:bottom;line-height:1.35}',
      '.kr-table tbody td{padding:12px 14px;border-top:1px solid var(--kr-rule);vertical-align:top;min-width:120px}',
      '.kr-table tbody td:first-child{min-width:110px;color:var(--kr-ink)}',
      '.kr-table tbody tr:nth-child(even) td{background:var(--kr-soft)}',
      '.kr-table tbody tr:hover td{background:var(--kr-accent-soft)}',
      '.kr-table-dense{font-size:13px}.kr-table-dense tbody td{min-width:105px}',
      '.kr-callout{border-left:4px solid var(--kr-accent);background:var(--kr-accent-soft);border-radius:0 12px 12px 0;padding:14px 18px;margin:18px 0 22px}',
      '.kr-callout p{margin:0}.kr-callout p+p{margin-top:8px}',
      '.kr-callout-label{font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;font-weight:700;color:var(--kr-accent-ink);margin-bottom:4px}',
      '.kr-callout-note{border-left-color:var(--kr-amber);background:var(--kr-amber-soft)}.kr-callout-note .kr-callout-label{color:#92400e}',
      '.kr-quote{border-left:3px solid var(--kr-rule);margin:16px 0;padding:4px 0 4px 16px;color:var(--kr-muted);font-style:italic}',
      '.kr-section hr{border:0;border-top:1px solid var(--kr-rule);margin:28px 0}',
      '.kr-section code{font-size:.88em;background:var(--kr-soft);padding:1px 5px;border-radius:4px}',
      'sup.kr-fn{font-size:.68em;line-height:0;vertical-align:super;margin-left:1px;font-weight:600;white-space:nowrap}',
      'sup.kr-fn a,sup.kr-fn span[data-fn]{color:var(--kr-accent);text-decoration:none;padding:0 1px}',
      'sup.kr-fn a:hover{text-decoration:underline}',
      'sup.kr-fn .kr-fn-sep{color:var(--kr-faint)}',
      '.kr-sources h2{font-size:20px;border-bottom-width:1px}',
      '.kr-sources-list{list-style:none;padding:0;margin:0;columns:1}',
      '.kr-sources-list li{display:flex;gap:12px;padding:10px 8px;border-bottom:1px solid var(--kr-rule);border-radius:8px;font-size:13.5px;line-height:1.5;transition:background .6s}',
      '.kr-src-num{flex:none;min-width:26px;height:22px;border-radius:5px;background:var(--kr-soft);border:1px solid var(--kr-rule);color:var(--kr-accent-ink);font-weight:700;font-size:11.5px;display:inline-flex;align-items:center;justify-content:center}',
      '.kr-src-body{min-width:0}.kr-src-title{font-weight:600;color:var(--kr-ink);overflow-wrap:anywhere}.kr-src-title a{color:var(--kr-ink);text-decoration:none}.kr-src-title a:hover{color:var(--kr-accent);text-decoration:underline}',
      '.kr-src-host{color:var(--kr-faint);font-size:12px}',
      '.kr-src-excerpt{margin:6px 0 0;padding-left:10px;border-left:2px solid var(--kr-rule);color:var(--kr-muted);font-style:italic;font-size:12.5px}',
      '.kr-src-intro{color:var(--kr-muted);font-size:13.5px}',
      '.kr-flash{background:#fef9c3 !important}',
      '.kr-disclaimer{margin-top:40px;padding:18px 20px;border-radius:12px;background:var(--kr-soft);border:1px solid var(--kr-rule)}',
      '.kr-disclaimer .kr-label{color:var(--kr-muted)}.kr-disclaimer p{margin:0;font-size:12.5px;line-height:1.6;color:var(--kr-muted)}',
      '.kr-toc-link{display:flex;gap:10px;align-items:flex-start;padding:7px 10px;border-radius:10px;font-size:13px;line-height:1.4;color:#94a3b8;text-decoration:none;transition:all .15s}',
      '.kr-toc-link:hover{color:#e0e7ff;background:rgba(99,102,241,.12)}',
      '.kr-toc-link.kr-active{color:#fff;background:rgba(99,102,241,.22)}',
      '.kr-toc-num{flex:none;min-width:20px;height:20px;border-radius:5px;background:rgba(99,102,241,.25);color:#c7d2fe;font-size:11px;font-weight:700;display:inline-flex;align-items:center;justify-content:center}',
      '.kr-toc-dot{background:transparent;position:relative}.kr-toc-dot::after{content:"";width:6px;height:6px;border-radius:50%;background:#818cf8}',
      '@media (max-width:640px){.kr-report{font-size:15px}.kr-meta{grid-template-columns:repeat(2,minmax(0,1fr))}.kr-meta>div:nth-child(3){padding-left:0;border-left:0}.kr-meta>div:nth-child(n+3){border-top:1px solid var(--kr-rule)}.kr-exec{padding:4px 16px 10px}.kr-section h2{font-size:20px}}'
    ].join('\n');
    var el = document.createElement('style');
    el.id = 'kr-report-styles';
    el.textContent = css;
    document.head.appendChild(el);
  }

  // =================================================================== PDF
  var PDF_CHAR_MAP = {
    '→': '–', '⇒': '–', '←': '<-', '↔': '<->', '↑': 'up', '↓': 'down',
    '≥': '>=', '≤': '<=', '≠': '!=', '≈': '~', '₹': 'Rs ', '−': '-', '‐': '-',
    '‑': '-', '‒': '-', '―': '—', ' ': ' ', ' ': ' ', ' ': ' ', ' ': ' ',
    ' ': ' ', '​': '', '‌': '', '‍': '', '﻿': '', '′': "'", '″': '"',
    '✓': '', '✔': '', '✅': '', '✗': 'x', '❌': 'x', '▲': '^', '▼': 'v',
    'Δ': 'Delta ', 'β': 'beta', 'α': 'alpha', '•': '•', '●': '•', '◦': '-',
    '‧': '·', '⁄': '/'
  };
  var WINANSI_EXTRA = '€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ';

  function pdfText(s) {
    s = String(s == null ? '' : s);
    var out = '';
    for (var i = 0; i < s.length; i++) {
      var ch = s[i];
      var code = s.charCodeAt(i);
      if (PDF_CHAR_MAP.hasOwnProperty(ch)) { out += PDF_CHAR_MAP[ch]; continue; }
      if ((code >= 32 && code < 127) || (code >= 160 && code <= 255) || WINANSI_EXTRA.indexOf(ch) !== -1) { out += ch; continue; }
      if (code === 9 || code === 10) { out += ' '; continue; }
      var base = ch.normalize ? ch.normalize('NFKD').replace(/[̀-ͯ]/g, '') : '';
      if (base && /^[\x20-\x7e]+$/.test(base)) out += base;
      // anything else (emoji, CJK, surrogate halves) is dropped
    }
    return out;
  }

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = src; s.async = true;
      s.onload = resolve;
      s.onerror = function () { reject(new Error('Failed to load ' + src)); };
      document.head.appendChild(s);
    });
  }

  var pdfLibsPromise = null;
  function ensurePdfLibs() {
    if (global.jspdf && global.jspdf.jsPDF && global.jspdf.jsPDF.API && global.jspdf.jsPDF.API.autoTable) return Promise.resolve();
    if (!pdfLibsPromise) {
      pdfLibsPromise = loadScript('https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js')
        .then(function () { return loadScript('https://cdnjs.cloudflare.com/ajax/libs/jspdf-autotable/3.8.2/jspdf.plugin.autotable.min.js'); })
        .catch(function (e) { pdfLibsPromise = null; throw e; });
    }
    return pdfLibsPromise;
  }

  var C = {
    ink: [15, 23, 42], body: [51, 65, 85], muted: [100, 116, 139], faint: [148, 163, 184], rule: [226, 232, 240],
    soft: [248, 250, 252], accent: [79, 70, 229], accentInk: [55, 48, 163], accentSoft: [238, 242, 255],
    amber: [217, 119, 6], amberSoft: [255, 251, 235], amberInk: [146, 64, 14], white: [255, 255, 255],
    bandSub: [165, 180, 252]
  };
  var PT = 0.3528; // mm per pt

  function PdfWriter(doc, model) {
    this.doc = doc;
    this.model = model;
    this.pw = doc.internal.pageSize.getWidth();
    this.ph = doc.internal.pageSize.getHeight();
    this.M = { l: 18, r: 18, t: 24, b: 22 };
    this.W = this.pw - this.M.l - this.M.r;
    this.y = this.M.t;
    this.toc = [];
    this.fnLinks = [];
    this.srcPages = {};
  }

  PdfWriter.prototype.bottom = function () { return this.ph - this.M.b; };
  PdfWriter.prototype.newPage = function () { this.doc.addPage(); this.y = this.M.t; };
  PdfWriter.prototype.ensure = function (h) { if (this.y + h > this.bottom()) this.newPage(); };
  PdfWriter.prototype.atTop = function () { return this.y <= this.M.t + 0.5; };

  PdfWriter.prototype.setFont = function (st, size) {
    var style = st.b && st.i ? 'bolditalic' : st.b ? 'bold' : st.i ? 'italic' : 'normal';
    this.doc.setFont(st.code ? 'courier' : 'helvetica', style);
    this.doc.setFontSize(size);
  };

  // Accurate Helvetica advance widths. jsPDF's built-in Helvetica metrics are rounded to tens
  // (r=330 vs 333, Y=660 vs 667), so its widths run ~0.6% short; viewers draw true Helvetica,
  // which then overruns the space before the next style run. Arial is metric-compatible with
  // Helvetica, so measure on a canvas when Arial is present (checked against a known width).
  var metricCtx = null, arialOk = null, widthCache = {};
  function helveticaUnits(ch, bold) {
    if (arialOk === null) {
      try {
        metricCtx = document.createElement('canvas').getContext('2d');
        metricCtx.font = '1000px Arial';
        var w = metricCtx.measureText('Hamburgefonstiv').width; // Helvetica: 7558 units
        arialOk = Math.abs(w - 7558) < 25;
      } catch (e) { arialOk = false; }
    }
    if (!arialOk) return null;
    var key = (bold ? 'b' : 'n') + ch;
    if (widthCache[key] == null) {
      metricCtx.font = (bold ? 'bold ' : '') + '1000px Arial';
      widthCache[key] = metricCtx.measureText(ch).width / 1000;
    }
    return widthCache[key];
  }

  // Text width in mm for the current font, without kerning (text is emitted unkerned).
  PdfWriter.prototype.tw = function (text) {
    var doc = this.doc;
    var f = doc.getFont();
    var helv = /helvetica/i.test(f.fontName);
    var bold = /bold/i.test(f.fontStyle);
    var cache = this._cw || (this._cw = {});
    var m = cache[f.fontName + '/' + f.fontStyle] || (cache[f.fontName + '/' + f.fontStyle] = {});
    var units = 0;
    for (var i = 0; i < text.length; i++) {
      var ch = text[i];
      if (m[ch] == null) {
        var u = helv ? helveticaUnits(ch, bold) : null;
        m[ch] = u != null ? u : doc.getStringUnitWidth(ch) * (helv ? 1.012 : 1);
      }
      units += m[ch];
    }
    return units * doc.getFontSize() / doc.internal.scaleFactor;
  };

  // Collect styled runs from an inline DOM subtree.
  PdfWriter.prototype.runs = function (node, st, out) {
    st = st || {};
    out = out || [];
    var self = this;
    Array.prototype.forEach.call(node.childNodes, function (ch) {
      if (ch.nodeType === 3) {
        var t = ch.textContent.replace(/\s+/g, ' ');
        if (t) out.push({ t: t, b: st.b, i: st.i, code: st.code, link: st.link });
        return;
      }
      if (ch.nodeType !== 1) return;
      var tag = ch.tagName;
      if (tag === 'BR') { out.push({ br: true }); return; }
      if (tag === 'SUP' && ch.classList.contains('kr-fn')) {
        var nums = Array.prototype.map.call(ch.querySelectorAll('[data-fn]'), function (a) { return Number(a.getAttribute('data-fn')); });
        out.push({ t: nums.join(','), sup: true, fns: nums, linked: !!ch.querySelector('a') });
        return;
      }
      if (tag === 'UL' || tag === 'OL' || tag === 'TABLE') return; // block-level, handled by caller
      var next = { b: st.b, i: st.i, code: st.code, link: st.link };
      if (tag === 'STRONG' || tag === 'B') next.b = true;
      if (tag === 'EM' || tag === 'I') next.i = true;
      if (tag === 'CODE') next.code = true;
      if (tag === 'A') { var u = safeUrl(ch.getAttribute('href')); if (u) next.link = u; }
      self.runs(ch, next, out);
    });
    return out;
  };

  // Break runs into lines that fit `width` at `size` pt.
  PdfWriter.prototype.layout = function (runs, width, size) {
    var doc = this.doc, self = this;
    var lines = [], cur = [], curW = 0, pendingSpace = null;
    function flush() { lines.push(cur); cur = []; curW = 0; pendingSpace = null; }
    runs.forEach(function (r) {
      if (r.br) { flush(); return; }
      if (r.sup) {
        var ss = Math.max(5.5, size * 0.62);
        self.setFont({ b: true }, ss);
        var tsup = pdfText(r.t);
        var w = self.tw(tsup);
        if (curW + w > width + 3 && cur.length) flush();
        cur.push({ t: tsup, w: w, st: { b: true }, size: ss, sup: true, fns: r.fns, linked: r.linked });
        curW += w;
        pendingSpace = null;
        return;
      }
      var parts = pdfText(r.t).split(/( +)/);
      parts.forEach(function (p) {
        if (!p) return;
        self.setFont(r, size);
        if (/^ +$/.test(p)) {
          if (cur.length) pendingSpace = { t: ' ', w: self.tw(' '), space: true };
          return;
        }
        var w = self.tw(p);
        var sw = pendingSpace ? pendingSpace.w : 0;
        if (cur.length && curW + sw + w > width) { flush(); sw = 0; }
        if (w > width) { // hard-break very long tokens (URLs)
          var chunk = '';
          for (var k = 0; k < p.length; k++) {
            var test = chunk + p[k];
            if (self.tw(test) > width - curW && chunk) {
              cur.push({ t: chunk, w: self.tw(chunk), st: r, size: size });
              flush();
              chunk = p[k];
            } else chunk = test;
          }
          p = chunk; w = self.tw(p);
        }
        if (pendingSpace && cur.length) { cur.push(pendingSpace); curW += pendingSpace.w; }
        pendingSpace = null;
        cur.push({ t: p, w: w, st: r, size: size });
        curW += w;
      });
    });
    if (cur.length) lines.push(cur);
    return lines;
  };

  function sameStyle(a, b) {
    return !!a.b === !!b.b && !!a.i === !!b.i && !!a.code === !!b.code && (a.link || '') === (b.link || '');
  }

  // Merge consecutive words (and the spaces between them) that share a style into one
  // text op, so copy/paste and text extraction keep real spaces.
  function mergeLine(line) {
    var out = [];
    line.forEach(function (tok) {
      var prev = out[out.length - 1];
      if (tok.space) {
        if (prev && !prev.sup && !prev.space) { prev.t += ' '; prev.w += tok.w; prev.trail = (prev.trail || 0) + tok.w; }
        else out.push({ space: true, w: tok.w });
        return;
      }
      if (prev && !prev.sup && !tok.sup && !prev.space && sameStyle(prev.st, tok.st) && prev.size === tok.size) {
        prev.t += tok.t; prev.w += tok.w; prev.trail = 0;
        return;
      }
      out.push(Object.assign({}, tok));
    });
    return out;
  }

  PdfWriter.prototype.drawLine = function (line, x, baseline, size, color) {
    var doc = this.doc, self = this;
    mergeLine(line).forEach(function (tok) {
      if (tok.space) { x += tok.w; return; }
      self.setFont(tok.st, tok.size);
      if (tok.sup) {
        doc.setTextColor.apply(doc, C.accent);
        doc.text(tok.t, x, baseline - size * PT * 0.38);
        if (tok.linked) self.fnLinks.push({ page: doc.getCurrentPageInfo().pageNumber, x: x, y: baseline - size * PT * 1.0, w: tok.w, h: size * PT * 0.8, fn: tok.fns[0] });
      } else {
        doc.setTextColor.apply(doc, tok.st.link ? C.accent : (tok.st.b ? C.ink : color));
        doc.text(tok.t, x, baseline);
        if (tok.st.link) doc.link(x, baseline - size * PT * 0.8, tok.w, size * PT, { url: tok.st.link });
      }
      x += tok.w;
    });
  };

  // Paragraph-like block: wraps, paginates line by line.
  PdfWriter.prototype.text = function (runs, o) {
    o = o || {};
    var size = o.size || 9.5, color = o.color || C.body, indent = o.indent || 0;
    var lh = size * PT * (o.lh || 1.5);
    var lines = this.layout(trimRuns(runs), this.W - indent, size);
    var self = this;
    lines.forEach(function (line, i) {
      self.ensure(lh);
      if (i === 0 && o.onFirstLine) o.onFirstLine(self.y, lh);
      self.drawLine(line, self.M.l + indent, self.y + lh * 0.72, size, color);
      self.y += lh;
    });
    this.y += (o.after != null ? o.after : 2.6);
  };

  function trimRuns(runs) {
    runs = runs.slice();
    while (runs.length && !runs[0].br && !runs[0].sup && !runs[0].t.trim()) runs.shift();
    if (runs.length && runs[0].t && !runs[0].sup) runs[0] = Object.assign({}, runs[0], { t: runs[0].t.replace(/^\s+/, '') });
    var last = runs.length - 1;
    if (last >= 0 && runs[last].t && !runs[last].sup) runs[last] = Object.assign({}, runs[last], { t: runs[last].t.replace(/\s+$/, '') });
    return runs;
  }

  PdfWriter.prototype.h2 = function (title, num, kicker) {
    var doc = this.doc;
    if (!this.atTop()) this.y += 4;
    this.ensure(38);
    this.toc.push({ title: title, num: num, page: doc.getCurrentPageInfo().pageNumber });
    if (doc.outline) doc.outline.add(null, (num ? num + '. ' : '') + pdfText(title), { pageNumber: doc.getCurrentPageInfo().pageNumber });
    doc.setFont('helvetica', 'bold'); doc.setFontSize(7.5);
    doc.setTextColor.apply(doc, C.accent);
    doc.text(kicker || (num ? 'SECTION ' + num : 'OVERVIEW'), this.M.l, this.y + 3, { charSpace: 0.3 });
    this.y += 5.5;
    doc.setFont('helvetica', 'bold'); doc.setFontSize(15);
    doc.setTextColor.apply(doc, C.ink);
    var lines = doc.splitTextToSize(pdfText(title), this.W);
    var self = this;
    lines.forEach(function (l) { doc.text(l, self.M.l, self.y + 5.4); self.y += 6.8; });
    this.y += 1.2;
    doc.setDrawColor.apply(doc, C.rule); doc.setLineWidth(0.25);
    doc.line(this.M.l, this.y, this.M.l + this.W, this.y);
    doc.setDrawColor.apply(doc, C.accent); doc.setLineWidth(0.9);
    doc.line(this.M.l, this.y, this.M.l + 22, this.y);
    this.y += 6;
  };

  PdfWriter.prototype.h3 = function (node) {
    if (!this.atTop()) this.y += 2.5;
    this.ensure(18);
    this.text(this.runs(node, { b: true }), { size: 11, color: C.ink, lh: 1.35, after: 1.8 });
  };

  PdfWriter.prototype.h4 = function (node) {
    if (!this.atTop()) this.y += 1.5;
    this.ensure(14);
    this.text(this.runs(node, { b: true }), { size: 9.5, color: C.ink, lh: 1.35, after: 1.2 });
  };

  PdfWriter.prototype.list = function (node, level) {
    var self = this, doc = this.doc;
    var ordered = node.tagName === 'OL';
    var indent = 2 + level * 6;
    var n = Number(node.getAttribute('start') || 1);
    Array.prototype.forEach.call(node.children, function (li) {
      if (li.tagName !== 'LI') return;
      var runs = self.runs(li);
      var marker = ordered ? (n++) + '.' : (level ? '–' : '•');
      if (runs.some(function (r) { return r.t && r.t.trim(); })) {
        self.text(runs, {
          indent: indent + 5, after: 1.4,
          onFirstLine: function (y, lh) {
            doc.setFont('helvetica', 'bold'); doc.setFontSize(9.5);
            doc.setTextColor.apply(doc, C.accent);
            doc.text(marker, self.M.l + indent + (ordered ? 0 : 0.8), y + lh * 0.72);
          }
        });
      }
      Array.prototype.forEach.call(li.children, function (c) {
        if (c.tagName === 'UL' || c.tagName === 'OL') self.list(c, level + 1);
      });
    });
    if (!level) this.y += 1.4;
  };

  PdfWriter.prototype.callout = function (node, kind) {
    var doc = this.doc, self = this;
    var label = node.querySelector('.kr-callout-label');
    var size = 9.3, lh = size * PT * 1.5, pad = 4;
    var blocks = Array.prototype.filter.call(node.children, function (c) { return c !== label; });
    var laid = blocks.map(function (b) { return self.layout(trimRuns(self.runs(b)), self.W - 2 * pad - 2, size); });
    var nLines = laid.reduce(function (a, l) { return a + l.length; }, 0);
    var h = pad * 2 + (label ? 5 : 0) + nLines * lh + (laid.length - 1) * 1.5;
    var bg = kind === 'note' ? C.amberSoft : C.accentSoft;
    var bar = kind === 'note' ? C.amber : C.accent;
    var labInk = kind === 'note' ? C.amberInk : C.accentInk;
    if (h > this.bottom() - this.M.t) { // too tall to box on one page: fall back to plain text
      blocks.forEach(function (b) { self.text(self.runs(b)); });
      return;
    }
    this.y += 1;
    this.ensure(h + 2);
    doc.setFillColor.apply(doc, bg);
    doc.rect(this.M.l, this.y, this.W, h, 'F');
    doc.setFillColor.apply(doc, bar);
    doc.rect(this.M.l, this.y, 1.1, h, 'F');
    var y = this.y + pad;
    if (label) {
      doc.setFont('helvetica', 'bold'); doc.setFontSize(7);
      doc.setTextColor.apply(doc, labInk);
      doc.text(pdfText(label.textContent).toUpperCase(), this.M.l + pad + 2, y + 2.6, { charSpace: 0.25 });
      y += 5;
    }
    laid.forEach(function (lines, bi) {
      lines.forEach(function (line) {
        self.drawLine(line, self.M.l + pad + 2, y + lh * 0.72, size, C.ink);
        y += lh;
      });
      if (bi < laid.length - 1) y += 1.5;
    });
    this.y += h + 4.5;
  };

  function cellPlain(td) {
    var clone = td.cloneNode(true);
    clone.querySelectorAll('sup.kr-fn').forEach(function (s) {
      var nums = Array.prototype.map.call(s.querySelectorAll('[data-fn]'), function (a) { return a.getAttribute('data-fn'); });
      s.replaceWith(document.createTextNode(' [' + nums.join(',') + ']'));
    });
    clone.querySelectorAll('br').forEach(function (b) { b.replaceWith(document.createTextNode('\n')); });
    return pdfText(clone.textContent.replace(/[ \t]+/g, ' ').replace(/ ?\n ?/g, '\n').trim());
  }

  function cellAllBold(td) {
    var txt = td.textContent.replace(/\s+/g, '');
    if (!txt) return false;
    var bold = '';
    td.querySelectorAll('strong,b').forEach(function (s) { bold += s.textContent.replace(/\s+/g, ''); });
    var supTxt = '';
    td.querySelectorAll('sup.kr-fn').forEach(function (s) { supTxt += s.textContent.replace(/\s+/g, ''); });
    return bold.length >= txt.length - supTxt.length;
  }

  PdfWriter.prototype.table = function (table) {
    var doc = this.doc, self = this;
    var headRow = table.tHead && table.tHead.rows[0] ? table.tHead.rows[0] : null;
    var bodyRows = Array.prototype.slice.call(table.tBodies.length ? table.tBodies[0].rows : table.rows);
    if (!headRow && bodyRows.length) headRow = bodyRows.shift();
    var head = headRow ? [Array.prototype.map.call(headRow.cells, function (c) { return pdfText(c.textContent.trim()).toUpperCase(); })] : undefined;
    var body = bodyRows.map(function (r) {
      return Array.prototype.map.call(r.cells, function (td) {
        return { content: cellPlain(td), styles: cellAllBold(td) ? { fontStyle: 'bold', textColor: C.ink } : {} };
      });
    });
    var cols = head ? head[0].length : (body[0] || []).length;
    var fs = cols >= 5 ? 7.3 : cols === 4 ? 7.8 : 8.3;
    if (!this.atTop()) this.y += 1.5;
    this.ensure(22);
    doc.autoTable({
      head: head, body: body,
      startY: this.y,
      margin: { left: this.M.l, right: this.M.r, top: this.M.t + 2, bottom: this.M.b },
      theme: 'grid',
      showHead: 'everyPage',
      rowPageBreak: 'avoid',
      tableWidth: this.W,
      styles: { font: 'helvetica', fontSize: fs, cellPadding: { top: 2.1, bottom: 2.1, left: 2.4, right: 2.4 }, textColor: C.body, lineColor: C.rule, lineWidth: 0.2, valign: 'top', overflow: 'linebreak' },
      headStyles: { fillColor: C.ink, textColor: C.white, fontStyle: 'bold', fontSize: Math.max(6.6, fs - 0.8), valign: 'bottom', lineColor: C.ink },
      alternateRowStyles: { fillColor: C.soft },
      columnStyles: { 0: { cellWidth: cols >= 4 ? Math.min(32, this.W * 0.2) : 'auto', textColor: C.ink } },
      didDrawPage: function () { /* headers/footers are drawn once all pages exist */ }
    });
    this.y = doc.lastAutoTable.finalY + 5.5;
  };

  PdfWriter.prototype.block = function (node) {
    var tag = node.tagName;
    if (!tag) return;
    if (tag === 'SECTION') { Array.prototype.forEach.call(node.children, this.block.bind(this)); return; }
    if (tag === 'H2') return this.h2(node.getAttribute('data-title') || node.textContent.trim(), node.getAttribute('data-num'));
    if (tag === 'H3') return this.h3(node);
    if (tag === 'H4' || tag === 'H5' || tag === 'H6') return this.h4(node);
    if (tag === 'P') {
      var lead = node.parentNode && node.parentNode.classList && node.parentNode.classList.contains('kr-exec') &&
        node === node.parentNode.querySelector('p');
      return this.text(this.runs(node), lead ? { size: 10.4, color: C.ink, after: 3.4 } : {});
    }
    if (tag === 'UL' || tag === 'OL') return this.list(node, 0);
    if (tag === 'DIV' && node.classList.contains('kr-table-wrap')) { var t = node.querySelector('table'); if (t) this.table(t); return; }
    if (tag === 'TABLE') return this.table(node);
    if (tag === 'DIV' && node.classList.contains('kr-callout')) return this.callout(node, node.classList.contains('kr-callout-note') ? 'note' : 'takeaway');
    if (tag === 'BLOCKQUOTE') return this.text(this.runs(node, { i: true }), { indent: 4, color: C.muted });
    if (tag === 'HR') { this.ensure(6); this.doc.setDrawColor.apply(this.doc, C.rule); this.doc.setLineWidth(0.25); this.doc.line(this.M.l, this.y + 2, this.M.l + this.W, this.y + 2); this.y += 6; return; }
    if (tag === 'PRE') return this.text([{ t: node.textContent, code: true }], { size: 8, color: C.body });
    if (node.children.length) Array.prototype.forEach.call(node.children, this.block.bind(this));
    else if (node.textContent.trim()) this.text(this.runs(node));
  };

  PdfWriter.prototype.sources = function () {
    var doc = this.doc, self = this, model = this.model;
    var list = model.sources.length ? model.sources : model.consulted.map(function (s, i) { return Object.assign({ num: i + 1 }, s); });
    this.newPage();
    this.h2('Sources & References', null, 'APPENDIX');
    if (!list.length) {
      this.text([{ t: 'Source links are not available for this report' + (model.legacy ? ' (it was saved before source tracking was added).' : '.') }], { color: C.muted });
      return;
    }
    if (!model.sources.length) this.text([{ t: 'Sources consulted by the research engine:' }], { color: C.muted, size: 8.5 });
    var numW = 9, w = this.W - numW;
    list.forEach(function (s) {
      var title = pdfText(s.title || hostOf(s.url) || 'Source ' + s.num);
      doc.setFont('helvetica', 'bold'); doc.setFontSize(8.4);
      var tl = doc.splitTextToSize(title, w).slice(0, 3);
      doc.setFont('helvetica', 'italic'); doc.setFontSize(7.4);
      var ex = s.excerpt ? doc.splitTextToSize('“' + pdfText(s.excerpt.length > 260 ? s.excerpt.slice(0, 257) + '…' : s.excerpt) + '”', w).slice(0, 3) : [];
      var h = tl.length * 3.7 + (s.url ? 3.6 : 0) + ex.length * 3.2 + 3.4;
      self.ensure(h);
      self.srcPages[s.num] = doc.getCurrentPageInfo().pageNumber;
      var y = self.y + 3;
      doc.setFont('helvetica', 'bold'); doc.setFontSize(8);
      doc.setTextColor.apply(doc, C.accent);
      doc.text('[' + s.num + ']', self.M.l, y);
      doc.setFontSize(8.4); doc.setTextColor.apply(doc, C.ink);
      tl.forEach(function (l) { doc.text(l, self.M.l + numW, y); y += 3.7; });
      if (s.url) {
        doc.setFont('helvetica', 'normal'); doc.setFontSize(7.2);
        doc.setTextColor.apply(doc, C.accent);
        var u = pdfText(s.url);
        while (u.length > 20 && doc.getTextWidth(u) > w) u = u.slice(0, -4);
        if (u !== pdfText(s.url)) u = u.slice(0, -1) + '…';
        doc.textWithLink(u, self.M.l + numW, y - 0.3, { url: s.url });
        y += 3.6;
      }
      if (ex.length) {
        doc.setFont('helvetica', 'italic'); doc.setFontSize(7.4);
        doc.setTextColor.apply(doc, C.muted);
        ex.forEach(function (l) { doc.text(l, self.M.l + numW, y - 0.2); y += 3.2; });
      }
      self.y = y + 0.4;
      doc.setDrawColor.apply(doc, C.rule); doc.setLineWidth(0.15);
      doc.line(self.M.l + numW, self.y - 1.2, self.M.l + self.W, self.y - 1.2);
      self.y += 1;
    });
  };

  PdfWriter.prototype.disclaimer = function () {
    this.y += 6;
    this.ensure(34);
    var doc = this.doc;
    doc.setFont('helvetica', 'bold'); doc.setFontSize(7.5);
    doc.setTextColor.apply(doc, C.muted);
    doc.text('IMPORTANT INFORMATION', this.M.l, this.y + 3, { charSpace: 0.25 });
    this.y += 5.5;
    this.text([{ t: DISCLAIMER }], { size: 7.8, color: C.muted, lh: 1.45 });
  };

  PdfWriter.prototype.cover = function (tocCount) {
    var doc = this.doc, m = this.model, M = this.M, W = this.W, self = this;
    var bandH = 104;
    doc.setFillColor.apply(doc, C.ink);
    doc.rect(0, 0, this.pw, bandH, 'F');
    doc.setFillColor.apply(doc, C.accent);
    doc.rect(0, bandH, this.pw, 1.6, 'F');

    doc.setFont('helvetica', 'bold'); doc.setFontSize(11);
    doc.setTextColor.apply(doc, C.white);
    doc.text('KAGGER.AI', M.l, 24, { charSpace: 0.3 });
    doc.setFontSize(7.5); doc.setTextColor.apply(doc, C.bandSub);
    doc.text('DEEP RESEARCH REPORT', this.pw - M.r, 24, { align: 'right', charSpace: 0.2 });
    doc.setDrawColor(51, 65, 85); doc.setLineWidth(0.25);
    doc.line(M.l, 29, this.pw - M.r, 29);

    var size = 24;
    doc.setFont('helvetica', 'bold'); doc.setFontSize(size);
    var tl = doc.splitTextToSize(pdfText(m.title), W);
    while (tl.length > 4 && size > 16) { size -= 2; doc.setFontSize(size); tl = doc.splitTextToSize(pdfText(m.title), W); }
    if (tl.length > 4) { tl = tl.slice(0, 4); tl[3] = tl[3].replace(/\s+\S*$/, '') + '…'; }
    doc.setTextColor.apply(doc, C.white);
    var lineH = size * PT * 1.22;
    var ty = bandH - 22 - (tl.length - 1) * lineH;
    tl.forEach(function (l, i) { doc.text(l, M.l, ty + i * lineH); });
    doc.setFont('helvetica', 'normal'); doc.setFontSize(9.5);
    doc.setTextColor.apply(doc, C.bandSub);
    doc.text(pdfText(m.depthLabel + '  ·  ' + formatDate(m.generatedAt)), M.l, bandH - 10);

    var y = bandH + 14;
    if (m.question) {
      doc.setFont('helvetica', 'bold'); doc.setFontSize(7.5);
      doc.setTextColor.apply(doc, C.accent);
      doc.text('RESEARCH QUESTION', M.l, y, { charSpace: 0.3 });
      y += 5;
      doc.setFont('helvetica', 'italic'); doc.setFontSize(9.8);
      doc.setTextColor.apply(doc, C.body);
      var ql = doc.splitTextToSize(pdfText(m.question), W);
      if (ql.length > 7) { ql = ql.slice(0, 7); ql[6] = ql[6].replace(/\s+\S*$/, '') + '…'; }
      ql.forEach(function (l) { doc.text(l, M.l, y); y += 4.8; });
      y += 4;
    }

    var stats = [
      ['PUBLISHED', formatDate(m.generatedAt)],
      ['RESEARCH DEPTH', m.depthLabel],
      ['SOURCES CITED', String(m.sources.length || m.consulted.length || '—')],
      ['READING TIME', m.readMinutes + ' min']
    ];
    doc.setDrawColor.apply(doc, C.rule); doc.setLineWidth(0.3);
    doc.line(M.l, y, M.l + W, y);
    var cw = W / 4;
    stats.forEach(function (s, i) {
      var x = M.l + i * cw + (i ? 4 : 0);
      if (i) doc.line(M.l + i * cw, y, M.l + i * cw, y + 18);
      doc.setFont('helvetica', 'bold'); doc.setFontSize(6.6);
      doc.setTextColor.apply(doc, C.faint);
      doc.text(s[0], x, y + 5.2, { charSpace: 0.2 });
      doc.setFontSize(9.6); doc.setTextColor.apply(doc, C.ink);
      doc.splitTextToSize(pdfText(s[1]), cw - 6).slice(0, 2).forEach(function (l, li) { doc.text(l, x, y + 10.6 + li * 4.2); });
    });
    y += 18;
    doc.line(M.l, y, M.l + W, y);
    y += 11;

    // Contents go on the cover when they fit; otherwise on a dedicated page 2.
    var tocRowH = 6.6;
    var avail = this.ph - 34 - y;
    this.tocOnCover = tocCount * tocRowH + 8 <= avail;
    this.tocY = y;
    this.tocRowH = tocRowH;

    doc.setFont('helvetica', 'normal'); doc.setFontSize(6.8);
    doc.setTextColor.apply(doc, C.faint);
    var disc = doc.splitTextToSize('For information and research purposes only. Not investment advice. Generated by the Kagger.AI autonomous research engine from public sources; verify against primary filings.', W);
    disc.forEach(function (l, i) { doc.text(l, M.l, self.ph - 16 + i * 3); });
  };

  PdfWriter.prototype.drawToc = function () {
    var doc = this.doc, self = this, M = this.M, W = this.W;
    var page = this.tocOnCover ? 1 : 2;
    doc.setPage(page);
    var y = this.tocOnCover ? this.tocY : M.t + 4;
    doc.setFont('helvetica', 'bold'); doc.setFontSize(7.5);
    doc.setTextColor.apply(doc, C.accent);
    doc.text('CONTENTS', M.l, y, { charSpace: 0.3 });
    y += 6;
    this.toc.forEach(function (e) {
      var label = (e.num ? e.num + '.' : '') ;
      doc.setFont('helvetica', 'bold'); doc.setFontSize(9);
      doc.setTextColor.apply(doc, C.accent);
      if (label) doc.text(label, M.l, y);
      doc.setFont('helvetica', e.num ? 'normal' : 'bold');
      doc.setTextColor.apply(doc, C.ink);
      var pn = String(e.page);
      var maxW = W - 9 - 14;
      var t = pdfText(e.title);
      while (doc.getTextWidth(t) > maxW && t.length > 8) t = t.slice(0, -2);
      if (t !== pdfText(e.title)) t = t.replace(/\s*\S?$/, '') + '…';
      doc.text(t, M.l + 9, y);
      var tw = self.tw(t);
      doc.setTextColor.apply(doc, C.faint);
      doc.text(pn, M.l + W, y, { align: 'right' });
      // dotted leader
      var x0 = M.l + 9 + tw + 2, x1 = M.l + W - self.tw(pn) - 2;
      doc.setFillColor.apply(doc, C.faint);
      for (var x = x0; x < x1; x += 1.6) doc.circle(x, y - 0.8, 0.12, 'F');
      doc.link(M.l, y - 4, W, 5.6, { pageNumber: e.page });
      y += self.tocRowH;
    });
  };

  PdfWriter.prototype.chrome = function () {
    var doc = this.doc, n = doc.getNumberOfPages(), M = this.M, pw = this.pw, ph = this.ph, m = this.model;
    var date = formatDate(m.generatedAt);
    for (var p = 2; p <= n; p++) {
      doc.setPage(p);
      doc.setFont('helvetica', 'bold'); doc.setFontSize(7.2);
      doc.setTextColor.apply(doc, C.accent);
      doc.text('KAGGER.AI', M.l, 13, { charSpace: 0.3 });
      var bw = this.tw('KAGGER.AI') + 0.3 * 9 + 2.5;
      doc.setFont('helvetica', 'normal'); doc.setTextColor.apply(doc, C.faint);
      doc.text('Deep Research Report', M.l + bw, 13);
      var t = pdfText(m.title), maxW = this.W * 0.55;
      while (doc.getTextWidth(t) > maxW && t.length > 10) t = t.slice(0, -2);
      if (t !== pdfText(m.title)) t = t.replace(/\s*\S?$/, '') + '…';
      doc.setTextColor.apply(doc, C.muted);
      doc.text(t, pw - M.r, 13, { align: 'right' });
      doc.setDrawColor.apply(doc, C.rule); doc.setLineWidth(0.25);
      doc.line(M.l, 16, pw - M.r, 16);

      doc.line(M.l, ph - 14, pw - M.r, ph - 14);
      doc.setFontSize(6.8); doc.setTextColor.apply(doc, C.faint);
      doc.text(pdfText(date + '  ·  For information only — not investment advice'), M.l, ph - 9.5);
      doc.setFont('helvetica', 'bold'); doc.setTextColor.apply(doc, C.muted);
      doc.text('Page ' + p + ' of ' + n, pw - M.r, ph - 9.5, { align: 'right' });
    }
  };

  PdfWriter.prototype.resolveFnLinks = function () {
    var doc = this.doc, self = this;
    this.fnLinks.forEach(function (l) {
      var target = self.srcPages[l.fn];
      if (!target) return;
      doc.setPage(l.page);
      doc.link(l.x, l.y, Math.max(l.w, 1.5), l.h + 1, { pageNumber: target });
    });
  };

  function slug(s) {
    var full = pdfText(s).replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
    if (full.length <= 80) return full;
    var cut = full.slice(0, 80);
    return cut.slice(0, cut.lastIndexOf('_') > 40 ? cut.lastIndexOf('_') : 80);
  }

  function pdfFilename(model) {
    var d = new Date(model.generatedAt);
    var ds = isNaN(d) ? new Date().toISOString().slice(0, 10) : d.toISOString().slice(0, 10);
    return 'Kagger_Research_' + (slug(model.title) || 'Report') + '_' + ds + '.pdf';
  }

  function downloadPdf(model, opts) {
    opts = opts || {};
    return ensurePdfLibs().then(function () {
      var jsPDF = global.jspdf.jsPDF;
      var doc = new jsPDF({ unit: 'mm', format: 'a4', orientation: 'portrait', compress: true });
      doc.setProperties({
        title: pdfText(model.title),
        subject: pdfText(model.question).slice(0, 500),
        author: 'Kagger.AI',
        creator: 'Kagger.AI Deep Research',
        keywords: 'Kagger.AI, equity research, Indian stock market'
      });
      var w = new PdfWriter(doc, model);
      var tocCount = model.headings.length + 1;
      w.cover(tocCount);
      if (!w.tocOnCover) doc.addPage(); // reserved contents page
      w.newPage();
      Array.prototype.forEach.call(model.body.children, function (sec) { w.block(sec); });
      w.sources();
      w.disclaimer();
      w.drawToc();
      w.resolveFnLinks();
      w.chrome();
      var name = pdfFilename(model);
      if (opts.returnDoc) return { doc: doc, filename: name };
      doc.save(name);
      return { filename: name };
    });
  }

  // Keep stored output small: one excerpt per citation, trimmed.
  function compactOutput(output) {
    if (!output || typeof output !== 'object') return output;
    return {
      type: output.type,
      content: output.content,
      basis: (Array.isArray(output.basis) ? output.basis : []).map(function (b) {
        return {
          field: b.field,
          citations: (b.citations || []).map(function (c) {
            var ex = Array.isArray(c.excerpts) ? c.excerpts.filter(Boolean)[0] : '';
            return { url: c.url, title: c.title || '', excerpts: ex ? [String(ex).slice(0, 400)] : [] };
          })
        };
      })
    };
  }

  global.KaggerReport = {
    build: build,
    render: render,
    setTitle: setTitle,
    downloadPdf: downloadPdf,
    compactOutput: compactOutput,
    deriveTitle: deriveTitle,
    smartTitle: smartTitle,
    pdfFilename: pdfFilename,
    _pdfText: pdfText
  };
})(window);
