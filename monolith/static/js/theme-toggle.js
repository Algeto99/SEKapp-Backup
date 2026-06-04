(function () {
    if (window.themeToggleInitialized) return;
    window.themeToggleInitialized = true;
    
    // App-wide layout guard: prevents stray horizontal overflow from revealing
    // a mismatched root background on mobile while preserving table wrappers.
    var style = document.createElement('style');
    style.textContent = [
        'html,body{',
        '  min-width:0;',
        '  max-width:100%;',
        '  overflow-x:hidden;',
        '}',
        'body{',
        '  min-height:100vh;',
        '}',
        '*,*::before,*::after{',
        '  box-sizing:border-box;',
        '}',
        'img,svg,canvas,video{',
        '  max-width:100%;',
        '}',
        '.container,.main-container,.container-card,.form-section,.card,.modal,.modal-box,.modal-container,.filter-bar{',
        '  max-width:100%;',
        '}',
        '.form-section,.card,.container-card,.modal,.modal-box,.modal-container{',
        '  overflow-wrap:anywhere;',
        '}',
        '.table-wrap,.table-wrapper,.table-container,.matrix-table-wrapper,.modal-table-wrapper,.drv-table-wrap{',
        '  max-width:100%;',
        '  overflow-x:auto;',
        '}',
        '@media (max-width:640px){',
        '  .form-section,.container-card{',
        '    overflow-x:hidden;',
        '  }',
        '}',
        '.theme-toggle{',
        '  position:fixed!important;',
        '  top:1.5rem!important;',
        '  right:1.5rem!important;',
        '  left:auto!important;',
        '  bottom:auto!important;',
        '  cursor:grab!important;',
        '  user-select:none;',
        '  -webkit-user-select:none;',
        '  touch-action:none;',
        '  z-index:9999!important;',
        '}',
        '.theme-toggle.tt-dragging{',
        '  cursor:grabbing!important;',
        '  transition:none!important;',
        '  opacity:0.85;',
        '}'
    ].join('');
    document.head.appendChild(style);

    function syncRootBackground() {
        if (!document.body) return;

        var bg = window.getComputedStyle(document.body).backgroundColor;
        if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {
            document.documentElement.style.backgroundColor = bg;
        }
    }

    // --- Theme functions — defined here so they can run both immediately and after DOM load ---
    const _darkIcon  = function() { return document.getElementById('darkModeIcon');  };
    const _lightIcon = function() { return document.getElementById('lightModeIcon'); };

    function setDarkMode() {
        document.body.classList.remove('light-mode');
        var di = _darkIcon(),  li = _lightIcon();
        if (di) di.style.display = 'block';
        if (li) li.style.display = 'none';
        try { localStorage.setItem('theme', 'dark'); } catch(e) {}
        syncRootBackground();
        window.dispatchEvent(new CustomEvent('themechange', { detail: { dark: true } }));
    }

    function setLightMode() {
        document.body.classList.add('light-mode');
        var di = _darkIcon(),  li = _lightIcon();
        if (di) di.style.display = 'none';
        if (li) li.style.display = 'block';
        try { localStorage.setItem('theme', 'light'); } catch(e) {}
        syncRootBackground();
        window.dispatchEvent(new CustomEvent('themechange', { detail: { dark: false } }));
    }

    function toggleTheme() {
        if (document.body.classList.contains('light-mode')) setDarkMode(); else setLightMode();
    }

    function initializeTheme() {
        var savedTheme  = localStorage.getItem('theme');
        var prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        if (savedTheme === 'light' || (!savedTheme && !prefersDark)) {
            setLightMode();
        } else {
            setDarkMode();
        }
    }

    // Expose globally so templates can call them directly (e.g. dashboard inline scripts)
    window.setDarkMode      = setDarkMode;
    window.setLightMode     = setLightMode;
    window.toggleTheme      = toggleTheme;
    window.initializeTheme  = initializeTheme;

    // Apply saved theme immediately — body exists because this script is in <body>.
    // This eliminates FOUC for pages that load theme-toggle.js at end of body
    // (auth pages, landing, etc.). Pages using _header_nav.html are covered by
    // its own inline script that runs even earlier.
    if (document.body) {
        initializeTheme();
        syncRootBackground();
    }

    document.addEventListener('DOMContentLoaded', function () {
        syncRootBackground();
        setTimeout(syncRootBackground, 50);
        setTimeout(syncRootBackground, 300);

        try {
            new MutationObserver(syncRootBackground).observe(document.body, {
                attributes: true,
                attributeFilter: ['class', 'style']
            });
        } catch (e) {}

        // Re-apply to sync icon state in case icons weren't in DOM during early init
        initializeTheme();

        var btn = document.getElementById('themeToggle');

        if (!btn) return;
        
        btn.addEventListener('click', toggleTheme);

        // --- Restore saved position ---
        var saved = null;
        try { saved = JSON.parse(localStorage.getItem('themeTogglePos')); } catch (e) {}
        if (saved && saved.top != null && saved.left != null) {
            btn.style.cssText += ';top:' + saved.top + '!important;left:' + saved.left + '!important;right:auto!important;bottom:auto!important;';
        }

        // --- Dragging ---
        var isDragging = false;
        var startClientX, startClientY, startBtnLeft, startBtnTop;

        function getClient(e) {
            return e.touches ? { x: e.touches[0].clientX, y: e.touches[0].clientY }
                             : { x: e.clientX, y: e.clientY };
        }

        function onStart(e) {
            // Only left-button for mouse
            if (e.button !== undefined && e.button !== 0) return;

            var c = getClient(e);
            var rect = btn.getBoundingClientRect();
            startClientX  = c.x;
            startClientY  = c.y;
            startBtnLeft  = rect.left;
            startBtnTop   = rect.top;
            isDragging     = false;

            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup',   onEnd);
            document.addEventListener('touchmove', onMove, { passive: false });
            document.addEventListener('touchend',  onEnd);
        }

        function onMove(e) {
            var c  = getClient(e);
            var dx = c.x - startClientX;
            var dy = c.y - startClientY;

            if (!isDragging && (Math.abs(dx) > 4 || Math.abs(dy) > 4)) {
                isDragging = true;
                btn.classList.add('tt-dragging');
                // Switch from right-based to left-based so math is simple
                btn.style.setProperty('right',  'auto', 'important');
                btn.style.setProperty('bottom', 'auto', 'important');
                btn.style.setProperty('left', startBtnLeft + 'px', 'important');
                btn.style.setProperty('top',  startBtnTop  + 'px', 'important');
            }

            if (isDragging) {
                if (e.cancelable) e.preventDefault();
                var newLeft = Math.max(0, Math.min(window.innerWidth  - btn.offsetWidth,  startBtnLeft + dx));
                var newTop  = Math.max(0, Math.min(window.innerHeight - btn.offsetHeight, startBtnTop  + dy));
                btn.style.setProperty('left', newLeft + 'px', 'important');
                btn.style.setProperty('top',  newTop  + 'px', 'important');
            }
        }

        function onEnd() {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup',   onEnd);
            document.removeEventListener('touchmove', onMove);
            document.removeEventListener('touchend',  onEnd);
            btn.classList.remove('tt-dragging');

            if (isDragging) {
                isDragging = false;
                try {
                    localStorage.setItem('themeTogglePos', JSON.stringify({
                        top:  btn.style.top,
                        left: btn.style.left
                    }));
                } catch (e) {}

                // Block the click that fires after mouseup/touchend
                btn.addEventListener('click', function absorbClick(ev) {
                    ev.stopImmediatePropagation();
                    ev.preventDefault();
                    btn.removeEventListener('click', absorbClick, true);
                }, { capture: true, once: true });
            }
        }

        btn.addEventListener('mousedown',  onStart);
        btn.addEventListener('touchstart', onStart, { passive: true });
    });
})();
