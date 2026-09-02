/**
 * multi-photo-uploader.js
 * Unified Camera & Gallery multi-photo accumulator for SecApp forms.
 * Allows users to take multiple photos consecutively with the mobile camera
 * and select multiple images from the gallery, preserving all photos in the same record.
 */

(function () {
    'use strict';

    /**
     * Formats bytes to human readable string (KB, MB).
     */
    function formatFileSize(bytes) {
        if (!bytes || bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    }

    /**
     * Initializes a MultiPhotoUploader on a given container or element.
     * @param {HTMLElement|string} target - Container element or selector where uploader is mounted.
     * @param {Object} options - Configuration options.
     */
    function initMultiPhotoUploader(target, options) {
        const container = typeof target === 'string' ? document.querySelector(target) : target;
        if (!container) return null;

        const config = Object.assign({
            inputName: 'foto_evidencia',
            inputId: 'foto_evidencia_' + Math.random().toString(36).substring(2, 9),
            label: 'Foto/Evidencia',
            helpText: '📸 Puede tomar varias fotos consecutivas con la cámara o seleccionar múltiples archivos desde la galería. Todas se conservarán.',
            accept: 'image/*,application/pdf',
            existingUrls: [],
            onChange: null,
            maxFiles: 30
        }, options || {});

        // Parse existing URLs if given as newline-separated string
        let existingPhotos = [];
        if (typeof config.existingUrls === 'string') {
            existingPhotos = config.existingUrls.split('\n').map(u => u.trim()).filter(Boolean);
        } else if (Array.isArray(config.existingUrls)) {
            existingPhotos = config.existingUrls.filter(Boolean);
        }

        // Internal list of accumulated File objects
        const accumulatedFiles = [];

        // Build HTML UI
        container.innerHTML = `
            <div class="secapp-photo-uploader rounded-xl p-4 bg-gray-800/60 border border-gray-700 shadow-sm transition-all">
                <div class="flex items-center justify-between mb-3">
                    <span class="text-sm font-semibold text-gray-200">${config.label}</span>
                    <span class="secapp-photo-badge text-xs font-semibold px-2.5 py-0.5 rounded-full bg-blue-900/50 text-blue-300 border border-blue-600/30 hidden">
                        0 fotografías
                    </span>
                </div>

                <!-- Action buttons (Camera + Gallery) -->
                <div class="flex flex-wrap gap-2.5 mb-3">
                    <button type="button" class="btn-camera flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-700 active:scale-95 text-white font-medium text-sm transition shadow-sm cursor-pointer flex-1 sm:flex-none">
                        <svg class="w-5 h-5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z" />
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z" />
                        </svg>
                        <span>Tomar Foto</span>
                    </button>

                    <button type="button" class="btn-gallery flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg bg-gray-700 hover:bg-gray-600 active:scale-95 text-gray-200 border border-gray-600 font-medium text-sm transition shadow-sm cursor-pointer flex-1 sm:flex-none">
                        <svg class="w-5 h-5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
                        </svg>
                        <span>Galería / Archivos</span>
                    </button>
                </div>

                <!-- Hidden native inputs -->
                <!-- Camera trigger input -->
                <input type="file" class="input-camera-hidden" accept="image/*" capture="environment" style="display:none !important;" tabindex="-1">
                <!-- Gallery trigger input -->
                <input type="file" class="input-gallery-hidden" accept="${config.accept}" multiple style="display:none !important;" tabindex="-1">
                <!-- Real form submission input with synchronized files -->
                <input type="file" name="${config.inputName}" id="${config.inputId}" class="input-real-files" accept="${config.accept}" multiple style="display:none !important;" tabindex="-1">

                <!-- Existing photos in edit mode -->
                <div class="existing-photos-section mb-3 ${existingPhotos.length ? '' : 'hidden'}">
                    <p class="text-xs font-semibold text-gray-400 mb-2">Evidencias actuales registradas:</p>
                    <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-2.5 existing-grid"></div>
                </div>

                <!-- Preview Grid for newly selected photos -->
                <div class="new-photos-grid grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-2.5"></div>

                <p class="text-xs text-gray-400 mt-2.5 leading-relaxed">${config.helpText}</p>
            </div>
        `;

        const btnCamera = container.querySelector('.btn-camera');
        const btnGallery = container.querySelector('.btn-gallery');
        const inputCamera = container.querySelector('.input-camera-hidden');
        const inputGallery = container.querySelector('.input-gallery-hidden');
        const inputReal = container.querySelector('.input-real-files');
        const badge = container.querySelector('.secapp-photo-badge');
        const newGrid = container.querySelector('.new-photos-grid');
        const existingSection = container.querySelector('.existing-photos-section');
        const existingGrid = container.querySelector('.existing-grid');

        // Render existing photos if any
        if (existingPhotos.length) {
            existingPhotos.forEach((url, i) => {
                const isPdf = url.toLowerCase().includes('.pdf');
                const card = document.createElement('div');
                card.className = 'relative group rounded-lg overflow-hidden border border-gray-600 bg-gray-700/80 shadow aspect-square flex items-center justify-center';
                if (isPdf) {
                    card.innerHTML = `
                        <a href="${url}" target="_blank" rel="noopener noreferrer" class="flex flex-col items-center justify-center p-2 text-center text-xs text-gray-300 hover:text-white">
                            <span class="text-2xl mb-1">📄</span>
                            <span class="truncate max-w-full">PDF #${i + 1}</span>
                        </a>
                        <span class="absolute top-1 left-1 bg-gray-900/80 text-gray-300 text-[10px] px-1.5 py-0.5 rounded font-bold">Guardado</span>
                    `;
                } else {
                    card.innerHTML = `
                        <a href="${url}" target="_blank" rel="noopener noreferrer" class="w-full h-full block">
                            <img src="${url}" alt="Evidencia ${i + 1}" class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-200">
                        </a>
                        <span class="absolute top-1 left-1 bg-gray-900/80 text-gray-300 text-[10px] px-1.5 py-0.5 rounded font-bold">Guardada</span>
                    `;
                }
                existingGrid.appendChild(card);
            });
        }

        /**
         * Synchronizes accumulated File objects into the real input using DataTransfer
         */
        function syncRealInput() {
            try {
                const dt = new DataTransfer();
                accumulatedFiles.forEach(file => dt.items.add(file));
                inputReal.files = dt.files;
            } catch (err) {
                console.warn('DataTransfer sync error (fallback mode):', err);
            }

            // Update badge counter
            const total = accumulatedFiles.length;
            if (total > 0) {
                badge.textContent = `${total} fotografía${total > 1 ? 's' : ''}`;
                badge.classList.remove('hidden');
            } else {
                badge.classList.add('hidden');
            }

            if (typeof config.onChange === 'function') {
                config.onChange(accumulatedFiles);
            }
        }

        /**
         * Re-renders the thumbnails for newly accumulated photos
         */
        function renderPreviewGrid() {
            newGrid.innerHTML = '';
            accumulatedFiles.forEach((file, index) => {
                const card = document.createElement('div');
                card.className = 'relative group rounded-lg overflow-hidden border border-blue-500/40 bg-gray-700/90 shadow aspect-square flex flex-col justify-between p-1';

                const isImage = file.type.startsWith('image/');
                const isPdf = file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');

                if (isImage) {
                    const objectUrl = URL.createObjectURL(file);
                    card.innerHTML = `
                        <img src="${objectUrl}" alt="${file.name}" class="w-full h-full object-cover rounded">
                        <div class="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center pointer-events-none">
                            <span class="text-white text-xs font-semibold px-1.5 py-0.5 rounded bg-black/60">${formatFileSize(file.size)}</span>
                        </div>
                        <span class="absolute top-1.5 left-1.5 bg-blue-600/90 text-white text-[10px] px-1.5 py-0.5 rounded-full font-bold shadow-sm">
                            #${index + 1}
                        </span>
                        <button type="button" class="btn-remove-photo absolute top-1.5 right-1.5 bg-red-600 hover:bg-red-700 active:scale-90 text-white rounded-full w-6 h-6 flex items-center justify-center shadow transition-all cursor-pointer" title="Eliminar foto" data-index="${index}">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 18L18 6M6 6l12 12"></path></svg>
                        </button>
                    `;
                } else {
                    card.innerHTML = `
                        <div class="w-full h-full flex flex-col items-center justify-center p-2 text-center text-gray-200">
                            <span class="text-2xl mb-1">${isPdf ? '📄' : '📎'}</span>
                            <span class="text-[11px] font-medium truncate max-w-full">${file.name}</span>
                            <span class="text-[10px] text-gray-400 mt-0.5">${formatFileSize(file.size)}</span>
                        </div>
                        <span class="absolute top-1.5 left-1.5 bg-blue-600/90 text-white text-[10px] px-1.5 py-0.5 rounded-full font-bold shadow-sm">
                            #${index + 1}
                        </span>
                        <button type="button" class="btn-remove-photo absolute top-1.5 right-1.5 bg-red-600 hover:bg-red-700 active:scale-90 text-white rounded-full w-6 h-6 flex items-center justify-center shadow transition-all cursor-pointer" title="Eliminar archivo" data-index="${index}">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 18L18 6M6 6l12 12"></path></svg>
                        </button>
                    `;
                }

                // Delete button handler
                const btnRemove = card.querySelector('.btn-remove-photo');
                btnRemove.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const removeIdx = parseInt(btnRemove.dataset.index, 10);
                    accumulatedFiles.splice(removeIdx, 1);
                    syncRealInput();
                    renderPreviewGrid();
                });

                newGrid.appendChild(card);
            });
        }

        /**
         * Appends new files to the accumulated collection and updates UI
         */
        function addFiles(newFiles) {
            if (!newFiles || !newFiles.length) return;
            for (let i = 0; i < newFiles.length; i++) {
                if (accumulatedFiles.length >= config.maxFiles) {
                    alert(`Ha alcanzado el límite máximo de ${config.maxFiles} fotografías.`);
                    break;
                }
                accumulatedFiles.push(newFiles[i]);
            }
            syncRealInput();
            renderPreviewGrid();
        }

        // Camera button click triggers native camera input
        btnCamera.addEventListener('click', () => {
            inputCamera.click();
        });

        // Gallery button click triggers multi-picker input
        btnGallery.addEventListener('click', () => {
            inputGallery.click();
        });

        // Camera input change: append captured photo and reset value
        inputCamera.addEventListener('change', function () {
            if (this.files && this.files.length) {
                addFiles(this.files);
            }
            this.value = ''; // Reset so tapping camera again triggers change event properly
        });

        // Gallery input change: append selected photos and reset value
        inputGallery.addEventListener('change', function () {
            if (this.files && this.files.length) {
                addFiles(this.files);
            }
            this.value = ''; // Reset so picking again triggers change event properly
        });

        return {
            getFiles: () => accumulatedFiles,
            clear: () => {
                accumulatedFiles.length = 0;
                syncRealInput();
                renderPreviewGrid();
            },
            addFiles: addFiles
        };
    }

    // Expose globally
    window.SecappMultiPhotoUploader = {
        init: initMultiPhotoUploader
    };
})();
