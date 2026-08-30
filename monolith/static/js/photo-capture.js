/**
 * photo-capture.js — In-Device Camera & Gallery Photo Capture for SecApp Forms
 *
 * Provides:
 * 1. Live WebRTC camera viewfinder with environment/user camera switching & torch.
 * 2. Fallback to native device camera via capture="environment".
 * 3. File picker / gallery upload.
 * 4. Image preview, lightbox viewer, retake and delete actions.
 * 5. DataTransfer file assignment to standard HTML <input type="file">.
 * 6. Compatibility with edit mode (prefilled photos from GCS).
 */

(function () {
    'use strict';

    // State
    let activeCard = null;
    let activeStream = null;
    let availableVideoDevices = [];
    let currentDeviceIndex = 0;
    let isTorchOn = false;
    let isFrozen = false;
    let currentFacingMode = 'environment'; // 'environment' (rear) or 'user' (front)

    // DOM Elements Cache for Camera Modal
    let modalEl = null;
    let videoEl = null;
    let canvasEl = null;
    let modalTitleEl = null;
    let modalHintEl = null;
    let cameraViewEl = null;
    let reviewViewEl = null;
    let errorViewEl = null;
    let errorMsgEl = null;
    let switchCamBtn = null;
    let torchBtn = null;

    // Lightbox Modal
    let lightboxEl = null;
    let lightboxImgEl = null;
    let lightboxTitleEl = null;

    // ── Build HTML Structures ───────────────────────────────────────────────
    function ensureCameraModal() {
        if (modalEl) return;

        modalEl = document.createElement('div');
        modalEl.id = 'secappPhotoCameraModal';
        modalEl.className = 'secapp-camera-modal-overlay';
        modalEl.style.display = 'none';
        modalEl.innerHTML = `
            <div class="secapp-camera-modal-dialog">
                <div class="secapp-camera-header">
                    <div class="secapp-camera-title-wrap">
                        <span class="secapp-camera-badge">CÁMARA</span>
                        <h3 class="secapp-camera-title" id="secappCamTitle">Tomar Fotografía</h3>
                    </div>
                    <div class="secapp-camera-header-actions">
                        <button type="button" class="secapp-cam-tool-btn" id="secappCamTorchBtn" title="Encender / Apagar Linterna" style="display:none;">
                            ⚡
                        </button>
                        <button type="button" class="secapp-cam-tool-btn" id="secappCamSwitchBtn" title="Cambiar Cámara" style="display:none;">
                            🔄
                        </button>
                        <button type="button" class="secapp-cam-close-btn" id="secappCamCloseBtn" title="Cerrar">
                            ✕
                        </button>
                    </div>
                </div>

                <div class="secapp-camera-body">
                    <!-- Live Viewfinder Mode -->
                    <div class="secapp-cam-live-view" id="secappCamLiveView">
                        <video id="secappCamVideo" autoplay playsinline muted></video>
                        <div class="secapp-cam-overlay-guide">
                            <div class="secapp-cam-guide-box">
                                <span class="guide-corner tl"></span>
                                <span class="guide-corner tr"></span>
                                <span class="guide-corner bl"></span>
                                <span class="guide-corner br"></span>
                                <div class="secapp-cam-guide-text" id="secappCamHint">Alinee el vehículo dentro del recuadro</div>
                            </div>
                        </div>
                        <div class="secapp-cam-loading-spinner" id="secappCamLoading">
                            <div class="secapp-spinner"></div>
                            <span>Iniciando cámara...</span>
                        </div>
                    </div>

                    <!-- Snapshot Review Mode -->
                    <div class="secapp-cam-review-view" id="secappCamReviewView" style="display:none;">
                        <canvas id="secappCamCanvas"></canvas>
                        <div class="secapp-cam-review-banner">
                            <span>¿Desea usar esta fotografía?</span>
                        </div>
                    </div>

                    <!-- Error Mode -->
                    <div class="secapp-cam-error-view" id="secappCamErrorView" style="display:none;">
                        <div class="secapp-cam-error-icon">📷⚠️</div>
                        <h4 class="secapp-cam-error-title">No se pudo acceder a la cámara en vivo</h4>
                        <p class="secapp-cam-error-desc" id="secappCamErrorMsg">
                            El navegador no tiene permiso para la cámara o el dispositivo no admite transmisión en vivo.
                        </p>
                        <div class="secapp-cam-error-actions">
                            <button type="button" class="secapp-btn secapp-btn-primary" id="secappCamNativeBtn">
                                📱 Usar Cámara Nativa del Dispositivo
                            </button>
                            <button type="button" class="secapp-btn secapp-btn-secondary" id="secappCamGalleryBtn">
                                📁 Seleccionar de Galería / Archivos
                            </button>
                        </div>
                    </div>
                </div>

                <!-- Live Toolbar -->
                <div class="secapp-camera-footer" id="secappCamLiveFooter">
                    <button type="button" class="secapp-footer-btn" id="secappCamFooterNativeBtn" title="Usar cámara del sistema">
                        📱 Nativa
                    </button>
                    <button type="button" class="secapp-shutter-btn" id="secappCamShutterBtn" title="Capturar Foto">
                        <span class="secapp-shutter-inner"></span>
                    </button>
                    <button type="button" class="secapp-footer-btn" id="secappCamFooterGalleryBtn" title="Elegir archivo existente">
                        📁 Galería
                    </button>
                </div>

                <!-- Review Toolbar -->
                <div class="secapp-camera-footer" id="secappCamReviewFooter" style="display:none;">
                    <button type="button" class="secapp-btn secapp-btn-secondary" id="secappCamRetakeBtn">
                        🔄 Tomar de nuevo
                    </button>
                    <button type="button" class="secapp-btn secapp-btn-success" id="secappCamAcceptBtn">
                        ✓ Usar esta Foto
                    </button>
                </div>
            </div>
        `;

        document.body.appendChild(modalEl);

        // Cache references
        videoEl = document.getElementById('secappCamVideo');
        canvasEl = document.getElementById('secappCamCanvas');
        modalTitleEl = document.getElementById('secappCamTitle');
        modalHintEl = document.getElementById('secappCamHint');
        cameraViewEl = document.getElementById('secappCamLiveView');
        reviewViewEl = document.getElementById('secappCamReviewView');
        errorViewEl = document.getElementById('secappCamErrorView');
        errorMsgEl = document.getElementById('secappCamErrorMsg');
        switchCamBtn = document.getElementById('secappCamSwitchBtn');
        torchBtn = document.getElementById('secappCamTorchBtn');

        // Bind events
        document.getElementById('secappCamCloseBtn').addEventListener('click', closeCameraModal);
        document.getElementById('secappCamShutterBtn').addEventListener('click', captureSnapshot);
        document.getElementById('secappCamRetakeBtn').addEventListener('click', resumeLiveCamera);
        document.getElementById('secappCamAcceptBtn').addEventListener('click', acceptCapturedPhoto);

        switchCamBtn.addEventListener('click', switchCamera);
        torchBtn.addEventListener('click', toggleTorch);

        document.getElementById('secappCamNativeBtn').addEventListener('click', () => {
            closeCameraModal();
            if (activeCard) triggerNativeCamera(activeCard);
        });
        document.getElementById('secappCamFooterNativeBtn').addEventListener('click', () => {
            closeCameraModal();
            if (activeCard) triggerNativeCamera(activeCard);
        });

        document.getElementById('secappCamGalleryBtn').addEventListener('click', () => {
            closeCameraModal();
            if (activeCard) triggerFilePicker(activeCard);
        });
        document.getElementById('secappCamFooterGalleryBtn').addEventListener('click', () => {
            closeCameraModal();
            if (activeCard) triggerFilePicker(activeCard);
        });

        // Close on backdrop click
        modalEl.addEventListener('click', (e) => {
            if (e.target === modalEl) closeCameraModal();
        });

        // Close on Escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && modalEl.style.display !== 'none') {
                closeCameraModal();
            }
        });
    }

    function ensureLightboxModal() {
        if (lightboxEl) return;

        lightboxEl = document.createElement('div');
        lightboxEl.id = 'secappPhotoLightboxModal';
        lightboxEl.className = 'secapp-lightbox-overlay';
        lightboxEl.style.display = 'none';
        lightboxEl.innerHTML = `
            <div class="secapp-lightbox-dialog">
                <div class="secapp-lightbox-header">
                    <h3 class="secapp-lightbox-title" id="secappLightboxTitle">Vista de Fotografía</h3>
                    <button type="button" class="secapp-cam-close-btn" id="secappLightboxCloseBtn">✕</button>
                </div>
                <div class="secapp-lightbox-content">
                    <img id="secappLightboxImg" alt="Vista previa de alta resolución" src="">
                </div>
            </div>
        `;
        document.body.appendChild(lightboxEl);

        lightboxImgEl = document.getElementById('secappLightboxImg');
        lightboxTitleEl = document.getElementById('secappLightboxTitle');

        document.getElementById('secappLightboxCloseBtn').addEventListener('click', closeLightbox);
        lightboxEl.addEventListener('click', (e) => {
            if (e.target === lightboxEl) closeLightbox();
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && lightboxEl.style.display !== 'none') {
                closeLightbox();
            }
        });
    }

    function openLightbox(title, imgSrc) {
        ensureLightboxModal();
        lightboxTitleEl.textContent = title || 'Fotografía Registrada';
        lightboxImgEl.src = imgSrc;
        lightboxEl.style.display = 'flex';
    }

    function closeLightbox() {
        if (lightboxEl) {
            lightboxEl.style.display = 'none';
            if (lightboxImgEl) lightboxImgEl.src = '';
        }
    }

    // ── Camera Stream Management ─────────────────────────────────────────────
    async function openCameraModal(card) {
        activeCard = card;
        ensureCameraModal();

        const label = card.dataset.label || 'VISTA';
        const hint = card.dataset.hint || 'Alinee el vehículo dentro del recuadro';

        modalTitleEl.textContent = `Tomar Foto: ${label}`;
        modalHintEl.textContent = hint;

        cameraViewEl.style.display = 'block';
        reviewViewEl.style.display = 'none';
        errorViewEl.style.display = 'none';
        document.getElementById('secappCamLiveFooter').style.display = 'flex';
        document.getElementById('secappCamReviewFooter').style.display = 'none';
        document.getElementById('secappCamLoading').style.display = 'flex';

        modalEl.style.display = 'flex';
        isFrozen = false;
        isTorchOn = false;
        if (torchBtn) {
            torchBtn.style.display = 'none';
            torchBtn.classList.remove('active');
        }

        await discoverDevices();
        await startCameraStream();
    }

    function stopCameraStream() {
        if (activeStream) {
            try {
                activeStream.getTracks().forEach(t => t.stop());
            } catch (err) {
                console.warn('[photo-capture] Error stopping stream tracks:', err);
            }
            activeStream = null;
        }
        if (videoEl) {
            videoEl.srcObject = null;
        }
    }

    function closeCameraModal() {
        stopCameraStream();
        if (modalEl) modalEl.style.display = 'none';
        activeCard = null;
        isFrozen = false;
    }

    async function discoverDevices() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
            return;
        }
        try {
            const devices = await navigator.mediaDevices.enumerateDevices();
            availableVideoDevices = devices.filter(d => d.kind === 'videoinput');
            if (availableVideoDevices.length > 1 && switchCamBtn) {
                switchCamBtn.style.display = 'inline-flex';
            } else if (switchCamBtn) {
                switchCamBtn.style.display = 'none';
            }
        } catch (e) {
            console.warn('[photo-capture] Error enumerating devices:', e);
        }
    }

    async function startCameraStream() {
        stopCameraStream();
        document.getElementById('secappCamLoading').style.display = 'flex';

        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            showCameraError('Este navegador o conexión no admite acceso a la cámara en vivo (WebRTC). Utilice la cámara nativa del dispositivo.');
            return;
        }

        const constraints = {
            audio: false,
            video: {
                facingMode: { ideal: currentFacingMode },
                width: { ideal: 1920, min: 640 },
                height: { ideal: 1080, min: 480 },
            }
        };

        // If a specific device is selected
        if (availableVideoDevices.length > 0 && availableVideoDevices[currentDeviceIndex]?.deviceId) {
            constraints.video.deviceId = { exact: availableVideoDevices[currentDeviceIndex].deviceId };
        }

        try {
            const stream = await navigator.mediaDevices.getUserMedia(constraints);
            activeStream = stream;
            videoEl.srcObject = stream;

            await new Promise((resolve) => {
                videoEl.onloadedmetadata = () => {
                    videoEl.play().then(resolve).catch(resolve);
                };
            });

            document.getElementById('secappCamLoading').style.display = 'none';

            // Check if torch/flashlight is supported
            checkTorchSupport(stream);

        } catch (err) {
            console.error('[photo-capture] getUserMedia error:', err);
            // If device constraint failed, try generic fallback
            if (constraints.video.deviceId) {
                delete constraints.video.deviceId;
                try {
                    const fallbackStream = await navigator.mediaDevices.getUserMedia({
                        video: { facingMode: { ideal: currentFacingMode } }
                    });
                    activeStream = fallbackStream;
                    videoEl.srcObject = fallbackStream;
                    await videoEl.play();
                    document.getElementById('secappCamLoading').style.display = 'none';
                    checkTorchSupport(fallbackStream);
                    return;
                } catch (fallbackErr) {
                    console.error('[photo-capture] Fallback stream failed:', fallbackErr);
                }
            }

            let msg = 'No se pudo iniciar la cámara.';
            if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
                msg = 'Permiso denegado para acceder a la cámara. Por favor autorice el acceso a la cámara en los ajustes del navegador o use la cámara nativa.';
            } else if (err.name === 'NotFoundError' || err.name === 'DevicesNotFoundError') {
                msg = 'No se encontró ningún dispositivo de cámara disponible.';
            } else if (err.name === 'NotReadableError' || err.name === 'TrackStartError') {
                msg = 'La cámara está siendo utilizada por otra aplicación.';
            }
            showCameraError(msg);
        }
    }

    function checkTorchSupport(stream) {
        const track = stream.getVideoTracks()[0];
        if (!track || !track.getCapabilities) {
            if (torchBtn) torchBtn.style.display = 'none';
            return;
        }
        try {
            const capabilities = track.getCapabilities();
            if (capabilities.torch && torchBtn) {
                torchBtn.style.display = 'inline-flex';
            } else if (torchBtn) {
                torchBtn.style.display = 'none';
            }
        } catch {
            if (torchBtn) torchBtn.style.display = 'none';
        }
    }

    async function toggleTorch() {
        if (!activeStream) return;
        const track = activeStream.getVideoTracks()[0];
        if (!track) return;

        isTorchOn = !isTorchOn;
        try {
            await track.applyConstraints({
                advanced: [{ torch: isTorchOn }]
            });
            if (torchBtn) {
                torchBtn.classList.toggle('active', isTorchOn);
            }
        } catch (err) {
            console.warn('[photo-capture] Error toggling torch:', err);
            isTorchOn = false;
        }
    }

    async function switchCamera() {
        if (availableVideoDevices.length > 1) {
            currentDeviceIndex = (currentDeviceIndex + 1) % availableVideoDevices.length;
        } else {
            currentFacingMode = currentFacingMode === 'environment' ? 'user' : 'environment';
        }
        await startCameraStream();
    }

    function showCameraError(message) {
        document.getElementById('secappCamLoading').style.display = 'none';
        cameraViewEl.style.display = 'none';
        reviewViewEl.style.display = 'none';
        errorViewEl.style.display = 'flex';
        errorMsgEl.textContent = message;
        document.getElementById('secappCamLiveFooter').style.display = 'none';
        document.getElementById('secappCamReviewFooter').style.display = 'none';
    }

    function captureSnapshot() {
        if (!videoEl || !activeStream) return;

        const w = videoEl.videoWidth || 1280;
        const h = videoEl.videoHeight || 720;

        canvasEl.width = w;
        canvasEl.height = h;

        const ctx = canvasEl.getContext('2d');
        // Handle mirroring if using front camera
        if (currentFacingMode === 'user') {
            ctx.translate(w, 0);
            ctx.scale(-1, 1);
        }
        ctx.drawImage(videoEl, 0, 0, w, h);
        if (currentFacingMode === 'user') {
            ctx.setTransform(1, 0, 0, 1, 0, 0);
        }

        // Haptic feedback if supported
        if (navigator.vibrate) {
            try { navigator.vibrate(50); } catch (_) {}
        }

        isFrozen = true;
        cameraViewEl.style.display = 'none';
        reviewViewEl.style.display = 'flex';
        document.getElementById('secappCamLiveFooter').style.display = 'none';
        document.getElementById('secappCamReviewFooter').style.display = 'flex';
    }

    function resumeLiveCamera() {
        isFrozen = false;
        reviewViewEl.style.display = 'none';
        cameraViewEl.style.display = 'block';
        document.getElementById('secappCamLiveFooter').style.display = 'flex';
        document.getElementById('secappCamReviewFooter').style.display = 'none';
    }

    function acceptCapturedPhoto() {
        if (!canvasEl || !activeCard) return;

        canvasEl.toBlob((blob) => {
            if (!blob) {
                alert('Error al generar la imagen capturada.');
                return;
            }

            const fotoKey = activeCard.dataset.foto || 'foto';
            const fileName = `${fotoKey}_${Date.now()}.jpg`;
            const file = new File([blob], fileName, { type: 'image/jpeg' });

            applyFileToCard(activeCard, file);
            closeCameraModal();
        }, 'image/jpeg', 0.88);
    }

    // ── Input & File Triggers ────────────────────────────────────────────────
    function triggerNativeCamera(card) {
        const camInput = card.querySelector('.foto-native-camera-input');
        if (camInput) {
            camInput.value = '';
            camInput.click();
        } else {
            triggerFilePicker(card);
        }
    }

    function triggerFilePicker(card) {
        const fileInput = card.querySelector('.foto-file-input');
        if (fileInput) {
            fileInput.click();
        }
    }

    function applyFileToCard(card, file) {
        const fileInput = card.querySelector('.foto-file-input');
        if (!fileInput) return;

        // Assign file to input.files using DataTransfer
        try {
            const dt = new DataTransfer();
            dt.items.add(file);
            fileInput.files = dt.files;
        } catch (e) {
            console.warn('[photo-capture] DataTransfer not supported, falling back to manual binding:', e);
        }

        // Store file reference on card for fallback submit
        card._attachedFile = file;

        // Update card visual state
        updateCardWithImage(card, file);

        // Dispatch change event on input for standard listeners
        fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function updateCardWithImage(card, fileOrUrl) {
        const preview = card.querySelector('.foto-card-preview');
        const emptyWrap = card.querySelector('.foto-card-empty');
        const filledWrap = card.querySelector('.foto-card-filled');
        const estado = card.querySelector('.foto-card-state');

        card.classList.add('foto-lista');
        if (estado) estado.textContent = '✓ Registrada';

        let url = '';
        if (typeof fileOrUrl === 'string') {
            url = fileOrUrl;
        } else if (fileOrUrl instanceof Blob || fileOrUrl instanceof File) {
            if (preview && preview.src && preview.src.startsWith('blob:')) {
                URL.revokeObjectURL(preview.src);
            }
            url = URL.createObjectURL(fileOrUrl);
        }

        if (preview) {
            preview.src = url;
            preview.hidden = false;
        }

        if (emptyWrap) emptyWrap.style.display = 'none';
        if (filledWrap) filledWrap.style.display = 'flex';
    }

    function clearCardPhoto(card) {
        const fileInput = card.querySelector('.foto-file-input');
        const camInput = card.querySelector('.foto-native-camera-input');
        const preview = card.querySelector('.foto-card-preview');
        const emptyWrap = card.querySelector('.foto-card-empty');
        const filledWrap = card.querySelector('.foto-card-filled');
        const estado = card.querySelector('.foto-card-state');

        if (fileInput) fileInput.value = '';
        if (camInput) camInput.value = '';
        delete card._attachedFile;

        card.classList.remove('foto-lista');
        if (estado) estado.textContent = 'Pendiente';

        if (preview) {
            if (preview.src && preview.src.startsWith('blob:')) {
                URL.revokeObjectURL(preview.src);
            }
            preview.removeAttribute('src');
            preview.hidden = true;
        }

        if (emptyWrap) emptyWrap.style.display = 'flex';
        if (filledWrap) filledWrap.style.display = 'none';

        // Re-enable required in new mode if applicable
        if (fileInput && !window.EDIT_MODE) {
            fileInput.required = true;
        }

        if (fileInput) {
            fileInput.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }

    // ── Initialize Photo Cards ───────────────────────────────────────────────
    function initPhotoCards() {
        const cards = document.querySelectorAll('.foto-card');
        if (!cards.length) return;

        cards.forEach(card => {
            if (card.dataset.secappPhotoInit) return;
            card.dataset.secappPhotoInit = '1';

            const fotoKey = card.dataset.foto;
            const mainInput = card.querySelector('.foto-file-input');
            const camInput = card.querySelector('.foto-native-camera-input');
            const preview = card.querySelector('.foto-card-preview');

            // Button triggers
            const takePhotoBtn = card.querySelector('[data-action="take-photo"]');
            const uploadFileBtn = card.querySelector('[data-action="upload-file"]');
            const viewPhotoBtn = card.querySelector('[data-action="view-photo"]');
            const retakePhotoBtn = card.querySelector('[data-action="retake-photo"]');
            const changeFileBtn = card.querySelector('[data-action="change-file"]');
            const removePhotoBtn = card.querySelector('[data-action="remove-photo"]');

            if (takePhotoBtn) {
                takePhotoBtn.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    openCameraModal(card);
                });
            }

            if (uploadFileBtn) {
                uploadFileBtn.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    triggerFilePicker(card);
                });
            }

            if (viewPhotoBtn) {
                viewPhotoBtn.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    if (preview && preview.src) {
                        const title = card.dataset.label ? `Vista ${card.dataset.label}` : 'Fotografía';
                        openLightbox(title, preview.src);
                    }
                });
            }

            if (retakePhotoBtn) {
                retakePhotoBtn.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    openCameraModal(card);
                });
            }

            if (changeFileBtn) {
                changeFileBtn.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    triggerFilePicker(card);
                });
            }

            if (removePhotoBtn) {
                removePhotoBtn.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    clearCardPhoto(card);
                });
            }

            // Click preview directly to view
            if (preview) {
                preview.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    if (preview.src) {
                        const title = card.dataset.label ? `Vista ${card.dataset.label}` : 'Fotografía';
                        openLightbox(title, preview.src);
                    }
                });
            }

            // Native camera input change handler
            if (camInput) {
                camInput.addEventListener('change', () => {
                    const f = camInput.files && camInput.files[0];
                    if (f) {
                        applyFileToCard(card, f);
                    }
                });
            }

            // Main file input change handler
            if (mainInput) {
                mainInput.addEventListener('change', () => {
                    const f = mainInput.files && mainInput.files[0];
                    if (f) {
                        card._attachedFile = f;
                        updateCardWithImage(card, f);
                    } else if (!card._attachedFile && !card.dataset.prefilledUrl) {
                        clearCardPhoto(card);
                    }
                });
            }

            // Edit mode pre-population
            if (window.EDIT_MODE && window.EDIT_RECORD) {
                const col = `${fotoKey}_url`;
                const existingUrl = window.EDIT_RECORD[col];
                if (existingUrl) {
                    card.dataset.prefilledUrl = existingUrl;
                    updateCardWithImage(card, existingUrl);
                    const estado = card.querySelector('.foto-card-state');
                    if (estado) estado.textContent = '✓ Registrada (Existente)';
                    if (mainInput) mainInput.required = false;
                }
            }
        });
    }

    // Initialize on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initPhotoCards);
    } else {
        initPhotoCards();
    }

    // Export API to window for external scripting if needed
    window.SecAppPhotoCapture = {
        init: initPhotoCards,
        openCamera: openCameraModal,
        openLightbox: openLightbox,
    };
})();
