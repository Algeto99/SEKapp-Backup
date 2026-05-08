(function () {
    // Override inline position so button starts top-right by default
    var style = document.createElement('style');
    style.textContent = [
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

    document.addEventListener('DOMContentLoaded', function () {
        var btn = document.getElementById('themeToggle');
        if (!btn) return;

        // --- Restore saved position ---
        var saved = null;
        try { saved = JSON.parse(localStorage.getItem('themeTogglePos')); } catch (e) {}
        if (saved && saved.top != null && saved.left != null) {
            btn.style.cssText += ';top:' + saved.top + '!important;left:' + saved.left + '!important;right:auto!important;bottom:auto!important;';
        }

        // --- Dragging ---
        var isDragging = false;
        var moved = false;
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
            moved          = false;

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
                moved      = true;
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
