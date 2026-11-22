// scale_ui.js
//
// Windows laptops usually run at 125%+ display scaling, which makes the CSS
// viewport only ~1536px wide and the dashboards feel oversized. On those
// screens we render the page at 90% (and widen the max-width containers to
// compensate) so a laptop shows the same amount of content as a 1080p monitor.
//
// The compaction only applies where it helps:
//   * a desktop pointer (mouse / trackpad) - never on phones or tablets, whose
//     high pixel density is not "OS scaling";
//   * a wide viewport (>= 1280 CSS px). When the user zooms the browser in,
//     the viewport narrows, the compaction switches off and the zoom is
//     honoured instead of being shrunk back down.
// It is re-evaluated on resize/zoom.
(function () {
    var root = document.documentElement;
    var desktop = window.matchMedia ? window.matchMedia('(hover: hover) and (pointer: fine)') : null;

    var style = document.createElement('style');
    style.id = 'scale-ui-styles';
    style.textContent = `
        /* Reduce overall scale to 90% */
        html.scaled-ui body {
            zoom: 0.9;
        }
        /* Increase backbone elements' max-width to compensate (1 / 0.9 = 1.111) */
        html.scaled-ui .max-w-7xl { max-width: 88.88rem !important; } /* originally 80rem */
        html.scaled-ui .max-w-6xl { max-width: 80rem !important; }    /* originally 72rem */
        html.scaled-ui .max-w-5xl { max-width: 71.11rem !important; } /* originally 64rem */
        html.scaled-ui .max-w-4xl { max-width: 62.22rem !important; } /* originally 56rem */
        html.scaled-ui .max-w-3xl { max-width: 53.33rem !important; } /* originally 48rem */
        html.scaled-ui .max-w-2xl { max-width: 46.66rem !important; } /* originally 42rem */
        html.scaled-ui .max-w-xl { max-width: 40rem !important; }     /* originally 36rem */
        html.scaled-ui .max-w-lg { max-width: 35.55rem !important; }  /* originally 32rem */
        html.scaled-ui .max-w-md { max-width: 31.11rem !important; }  /* originally 28rem */
        html.scaled-ui .max-w-sm { max-width: 26.66rem !important; }  /* originally 24rem */
        html.scaled-ui .max-w-xs { max-width: 22.22rem !important; }  /* originally 20rem */

        /* Handle container classes correctly */
        @media (min-width: 640px) { html.scaled-ui .container { max-width: 711px !important; } }
        @media (min-width: 768px) { html.scaled-ui .container { max-width: 853px !important; } }
        @media (min-width: 1024px) { html.scaled-ui .container { max-width: 1137px !important; } }
        @media (min-width: 1280px) { html.scaled-ui .container { max-width: 1422px !important; } }
        @media (min-width: 1536px) { html.scaled-ui .container { max-width: 1706px !important; } }
    `;
    (document.head || root).appendChild(style);

    function evaluate() {
        var on = window.devicePixelRatio >= 1.25 &&
            (!desktop || desktop.matches) &&
            window.innerWidth >= 1280;
        if (root.classList.contains('scaled-ui') !== on) {
            root.classList.toggle('scaled-ui', on);
        }
    }

    evaluate();
    var pending = null;
    window.addEventListener('resize', function () {
        if (pending) cancelAnimationFrame(pending);
        pending = requestAnimationFrame(function () { pending = null; evaluate(); });
    });
})();
