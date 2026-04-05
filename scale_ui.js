// scale_ui.js
(function () {
    // Check if the Windows OS scale is 125% or higher
    // devicePixelRatio is 1 at 100%, 1.25 at 125%, 1.5 at 150%, etc.
    if (window.devicePixelRatio >= 1.25) {
        document.documentElement.classList.add('scaled-ui');

        // Add the CSS rules dynamically
        const style = document.createElement('style');
        style.innerHTML = `
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
        document.head.appendChild(style);
    }
})();
