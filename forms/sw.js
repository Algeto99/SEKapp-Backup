// sw.js - Enhanced Service Worker for iOS PWA compatibility with TRUE offline support
const CACHE_NAME = 'smt-secapp-v5';
const OFFLINE_URL = '/offline.html';

// Essential resources to cache (more aggressive for iOS)
const urlsToCache = [
  '/',
  '/manifest.json',
  '/offline.html',
  '/install',
  // External resources that are critical
  'https://cdn.tailwindcss.com',
  'https://cdn.jsdelivr.net/npm/tom-select@2.3.1/dist/css/tom-select.css',
  'https://cdn.jsdelivr.net/npm/tom-select@2.3.1/dist/js/tom-select.complete.min.js',
  'https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap',
  'https://fonts.googleapis.com/css2?family=Merriweather:wght@300;400;700&display=swap',
  'https://storage.googleapis.com/smt-misc/SMT-logo.png'
];

// Install event - cache resources immediately and skip waiting
self.addEventListener('install', event => {
  console.log('[SW] Installing service worker v5...');
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        console.log('[SW] Caching app shell and critical resources');
        
        // Cache the main form page first (most important)
        return cache.add('/').then(() => {
          console.log('[SW] Main form page cached successfully');
          
          // Then cache offline page
          return cache.add('/offline.html');
        }).then(() => {
          console.log('[SW] Offline page cached successfully');
          
          // Then try to cache external resources, but don't fail if they don't work
          const externalPromises = urlsToCache.slice(2).map(url => 
            cache.add(url).catch(err => {
              console.warn(`[SW] Failed to cache ${url}:`, err);
              return null;
            })
          );
          return Promise.allSettled(externalPromises);
        });
      })
      .then(() => {
        console.log('[SW] Installation complete, skipping waiting');
        return self.skipWaiting();
      })
      .catch(err => {
        console.error('[SW] Cache installation failed:', err);
      })
  );
});

// Activate event - claim clients immediately and clean old caches
self.addEventListener('activate', event => {
  console.log('[SW] Activating service worker v5...');
  event.waitUntil(
    Promise.all([
      // Clean up old caches
      caches.keys().then(cacheNames => {
        return Promise.all(
          cacheNames.map(cacheName => {
            if (cacheName !== CACHE_NAME) {
              console.log('[SW] Deleting old cache:', cacheName);
              return caches.delete(cacheName);
            }
          })
        );
      }),
      // Claim all clients immediately
      self.clients.claim()
    ]).then(() => {
      console.log('[SW] Service worker v5 activated and ready');
    })
  );
});

// Enhanced fetch event with better offline form handling
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  const request = event.request;
  
  // Skip non-GET requests for caching - LET FORM SUBMISSIONS GO THROUGH NORMALLY
  if (request.method !== 'GET') {
    // Don't intercept POST requests - let the main app handle offline form submissions
    return;
  }
  
  // Handle the main form page specifically (/ or root)
  if (url.pathname === '/' || url.pathname === '') {
    console.log('[SW] Handling request for main form page');
    event.respondWith(
      // Try network first for fresh content
      fetch(request, { cache: 'no-cache' })
        .then(response => {
          console.log('[SW] Network response for main page:', response.status);
          // If we get a good response, cache it and return it
          if (response.status === 200) {
            const responseClone = response.clone();
            caches.open(CACHE_NAME)
              .then(cache => {
                cache.put(request, responseClone);
                console.log('[SW] Updated cache with fresh main page');
              })
              .catch(err => console.warn('[SW] Failed to update cache:', err));
          }
          return response;
        })
        .catch(() => {
          console.log('[SW] Network failed for main page, trying cache');
          // Network failed, try cache
          return caches.match(request)
            .then(cachedResponse => {
              if (cachedResponse) {
                console.log('[SW] Serving main page from cache');
                return cachedResponse;
              }
              
              // No cache either, show offline page
              console.log('[SW] No cache found for main page, showing offline page');
              return caches.match('/offline.html')
                .then(offlineResponse => {
                  if (offlineResponse) {
                    return offlineResponse;
                  }
                  // Fallback offline page if /offline.html isn't cached
                  return new Response(
                    createFallbackOfflinePage(),
                    { 
                      status: 200,
                      headers: { 'Content-Type': 'text/html' }
                    }
                  );
                });
            });
        })
    );
    return;
  }
  
  // Handle other HTML pages
  if (request.destination === 'document' || 
      request.headers.get('accept')?.includes('text/html') ||
      url.pathname.includes('.html')) {
    
    event.respondWith(
      fetch(request)
        .then(response => {
          // If we get a good response, cache it and return it
          if (response.status === 200) {
            const responseClone = response.clone();
            caches.open(CACHE_NAME)
              .then(cache => cache.put(request, responseClone))
              .catch(err => console.warn('[SW] Failed to cache response:', err));
          }
          return response;
        })
        .catch(() => {
          // Network failed, try cache
          return caches.match(request)
            .then(cachedResponse => {
              if (cachedResponse) {
                console.log('[SW] Serving from cache:', request.url);
                return cachedResponse;
              }
              
              // No cache either, show offline page
              console.log('[SW] No cache found, showing offline page');
              return caches.match('/offline.html')
                .then(offlineResponse => {
                  if (offlineResponse) {
                    return offlineResponse;
                  }
                  // Fallback offline page if /offline.html isn't cached
                  return new Response(
                    createFallbackOfflinePage(),
                    { 
                      status: 200,
                      headers: { 'Content-Type': 'text/html' }
                    }
                  );
                });
            });
        })
    );
  }
  
  // Handle static assets (CSS, JS, images) - Cache First with Network Fallback
  else {
    event.respondWith(
      caches.match(request)
        .then(cachedResponse => {
          if (cachedResponse) {
            // Serve from cache immediately
            console.log('[SW] Cache hit for:', request.url);
            
            // Try to update cache in background for next time
            fetch(request)
              .then(response => {
                if (response.status === 200) {
                  const responseClone = response.clone();
                  caches.open(CACHE_NAME)
                    .then(cache => cache.put(request, responseClone));
                }
              })
              .catch(() => {
                // Background update failed, but we already have cached version
              });
            
            return cachedResponse;
          }
          
          // Not in cache, try network
          return fetch(request)
            .then(response => {
              // Cache successful responses
              if (response.status === 200) {
                const responseClone = response.clone();
                caches.open(CACHE_NAME)
                  .then(cache => cache.put(request, responseClone));
              }
              return response;
            })
            .catch(() => {
              console.log('[SW] Failed to fetch:', request.url);
              
              // For critical assets, return a placeholder
              if (request.url.includes('.css')) {
                return new Response('/* Offline - CSS not available */', {
                  status: 200,
                  headers: { 'Content-Type': 'text/css' }
                });
              }
              if (request.url.includes('.js')) {
                return new Response('console.log("Offline - JS not available");', {
                  status: 200,
                  headers: { 'Content-Type': 'application/javascript' }
                });
              }
              
              return new Response('Offline - asset not available', {
                status: 503,
                statusText: 'Service Unavailable'
              });
            });
        })
    );
  }
});

// Background sync for offline form submissions
self.addEventListener('sync', event => {
  if (event.tag === 'submit-reports') {
    event.waitUntil(submitSavedReports());
  }
});

// Handle offline form submissions stored in localStorage
async function submitSavedReports() {
  console.log('[SW] Attempting to sync saved reports');
  
  try {
    // Get saved reports from main thread
    const clients = await self.clients.matchAll();
    if (clients.length > 0) {
      clients[0].postMessage({
        type: 'REQUEST_OFFLINE_REPORTS'
      });
    }
  } catch (error) {
    console.log('[SW] Error syncing saved reports:', error);
  }
}

// Message handling for communication with main app
self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
  
  if (event.data && event.data.type === 'SYNC_REPORTS') {
    // Handle sync request from main thread
    const reports = event.data.reports;
    console.log('[SW] Received reports to sync:', Object.keys(reports).length);
    // Process reports sync...
  }
  
  if (event.data && event.data.type === 'FORCE_UPDATE') {
    // Force update the service worker
    self.skipWaiting();
  }
});

// Enhanced push notification handling (for future use)
self.addEventListener('push', event => {
  if (event.data) {
    const data = event.data.json();
    const options = {
      body: data.body,
      icon: 'https://storage.googleapis.com/smt-misc/SMT-logo.png',
      badge: 'https://storage.googleapis.com/smt-misc/SMT-logo.png',
      vibrate: [100, 50, 100],
      data: {
        dateOfArrival: Date.now(),
        primaryKey: data.primaryKey || '1'
      },
      actions: [
        {
          action: 'explore',
          title: 'Abrir App',
          icon: 'https://storage.googleapis.com/smt-misc/SMT-logo.png'
        },
        {
          action: 'close',
          title: 'Cerrar',
          icon: 'https://storage.googleapis.com/smt-misc/SMT-logo.png'
        }
      ]
    };
    
    event.waitUntil(
      self.registration.showNotification('SMT SecApp', options)
    );
  }
});

// Handle notification clicks
self.addEventListener('notificationclick', event => {
  event.notification.close();
  
  if (event.action === 'explore') {
    // Open the app
    event.waitUntil(
      clients.openWindow('/')
    );
  }
});

// Create a comprehensive fallback offline page if the cached one isn't available
function createFallbackOfflinePage() {
  return `
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sin Conexión - SMT SecApp</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background-color: #1a202c;
            color: #e2e8f0;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            padding: 20px;
        }
        .container {
            background-color: #2d3748;
            border-radius: 12px;
            padding: 40px;
            text-align: center;
            max-width: 400px;
            width: 100%;
            box-shadow: 0 8px 30px rgba(0, 0, 0, 0.5);
        }
        .icon {
            width: 60px;
            height: 60px;
            background-color: #f59e0b;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0 auto 20px;
            font-size: 24px;
        }
        h1 {
            color: #f59e0b;
            margin-bottom: 20px;
        }
        .status {
            background-color: #065f46;
            color: #10b981;
            padding: 15px;
            border-radius: 8px;
            margin: 20px 0;
            font-size: 14px;
        }
        .button {
            background-color: #3b82f6;
            color: white;
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            text-decoration: none;
            display: inline-block;
            margin: 10px;
            width: calc(100% - 20px);
            font-size: 16px;
        }
        .button:hover {
            background-color: #2563eb;
        }
        .logo {
            width: 80px;
            height: 80px;
            margin: 0 auto 20px;
            border-radius: 50%;
            background: url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="45" fill="%232563eb"/><text x="50" y="60" text-anchor="middle" fill="white" font-size="20" font-family="Arial">SMT</text></svg>') center/cover;
        }
        .offline-features {
            background-color: #374151;
            padding: 15px;
            border-radius: 8px;
            margin: 15px 0;
            text-align: left;
            font-size: 14px;
        }
        .offline-features h3 {
            margin-top: 0;
            color: #60a5fa;
        }
        .offline-features ul {
            margin: 10px 0;
            padding-left: 20px;
        }
        .offline-features li {
            margin: 5px 0;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="logo"></div>
        <h1>📱 SMT SecApp</h1>
        <div class="icon">📴</div>
        <h2>Sin Conexión</h2>
        <p>No tienes conexión a internet. La aplicación funciona offline pero no se puede acceder al formulario en este momento.</p>
        
        <div class="status">
            ❌ Formulario no disponible offline<br>
            🔄 Verifica tu conexión para acceder
        </div>
        
        <div class="offline-features">
            <h3>🚀 Para usar offline:</h3>
            <ul>
                <li>📱 Instala la aplicación en tu dispositivo</li>
                <li>🔄 Conecta a internet una vez para cargar el formulario</li>
                <li>📝 Luego podrás crear reportes sin conexión</li>
                <li>💾 Los reportes se guardarán automáticamente</li>
            </ul>
        </div>
        
        <button class="button" onclick="location.reload()">🔄 Verificar Conexión</button>
        
        <p style="font-size: 12px; color: #9ca3af; margin-top: 20px;">
            Estado: <span id="status">Verificando...</span><br>
            <span id="last-update">Última actualización: ${new Date().toLocaleTimeString()}</span>
        </p>
    </div>
    
    <script>
        function updateStatus() {
            const status = document.getElementById('status');
            const lastUpdate = document.getElementById('last-update');
            
            if (navigator.onLine) {
                status.textContent = '🟢 En línea';
                status.style.color = '#10b981';
                // Auto-redirect when connection is restored
                setTimeout(() => {
                    window.location.href = '/';
                }, 2000);
            } else {
                status.textContent = '🔴 Sin conexión';
                status.style.color = '#f59e0b';
            }
            
            lastUpdate.textContent = 'Última actualización: ' + new Date().toLocaleTimeString();
        }

        // Listen for connection changes
        window.addEventListener('online', updateStatus);
        window.addEventListener('offline', updateStatus);
        
        // Initial check
        updateStatus();
        
        // Check connection every 5 seconds
        setInterval(updateStatus, 5000);
        
        // iOS specific handling
        if (/iPad|iPhone|iPod/.test(navigator.userAgent)) {
            // Add iOS-specific styling or behavior
            document.body.style.webkitUserSelect = 'none';
            document.body.style.webkitTouchCallout = 'none';
        }
        
        // Service worker communication
        if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {
            navigator.serviceWorker.addEventListener('message', event => {
                if (event.data.type === 'CONNECTIVITY_CHANGE') {
                    updateStatus();
                }
            });
        }
    </script>
</body>
</html>
  `;
}

// Periodic cache cleanup and optimization
self.addEventListener('periodicsync', event => {
  if (event.tag === 'cache-cleanup') {
    event.waitUntil(performCacheCleanup());
  }
});

async function performCacheCleanup() {
  console.log('[SW] Performing periodic cache cleanup');
  
  try {
    const cache = await caches.open(CACHE_NAME);
    const requests = await cache.keys();
    
    // Remove old or unused cache entries
    const cleanupPromises = requests.map(async (request) => {
      const response = await cache.match(request);
      if (response) {
        const dateHeader = response.headers.get('date');
        if (dateHeader) {
          const responseDate = new Date(dateHeader);
          const now = new Date();
          const daysDiff = (now - responseDate) / (1000 * 60 * 60 * 24);
          
          // Remove cache entries older than 7 days for non-essential resources
          if (daysDiff > 7 && !urlsToCache.includes(request.url)) {
            console.log('[SW] Removing old cache entry:', request.url);
            return cache.delete(request);
          }
        }
      }
    });
    
    await Promise.all(cleanupPromises);
    console.log('[SW] Cache cleanup completed');
  } catch (error) {
    console.error('[SW] Cache cleanup failed:', error);
  }
}