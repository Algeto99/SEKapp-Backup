// sw.js - Enhanced Service Worker with Auto-Update Support
const CACHE_NAME = 'smt-secapp-v6'; // Increment version for updates
const OFFLINE_URL = '/offline.html';
const UPDATE_CHECK_INTERVAL = 60000; // Check for updates every minute
const BACKGROUND_SYNC_TAG = 'background-sync';

// Essential resources to cache
const urlsToCache = [
  '/',
  '/manifest.json',
  '/offline.html',
  '/install',
  'https://cdn.tailwindcss.com',
  'https://cdn.jsdelivr.net/npm/tom-select@2.3.1/dist/css/tom-select.css',
  'https://cdn.jsdelivr.net/npm/tom-select@2.3.1/dist/js/tom-select.complete.min.js',
  'https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap',
  'https://fonts.googleapis.com/css2?family=Merriweather:wght@300;400;700&display=swap',
  'https://storage.googleapis.com/smt-misc/SMT-logo.png'
];

// Install event - improved caching strategy
self.addEventListener('install', event => {
  console.log('[SW] Installing service worker v6...');
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        console.log('[SW] Caching app shell and critical resources');
        
        // Cache critical resources with error handling
        return Promise.allSettled(
          urlsToCache.map(url => 
            cache.add(url).catch(err => {
              console.warn(`[SW] Failed to cache ${url}:`, err);
              return null;
            })
          )
        );
      })
      .then(() => {
        console.log('[SW] Installation complete, skipping waiting');
        // Force immediate activation of new service worker
        return self.skipWaiting();
      })
      .catch(err => {
        console.error('[SW] Cache installation failed:', err);
      })
  );
});

// Activate event - clean old caches and claim clients immediately
self.addEventListener('activate', event => {
  console.log('[SW] Activating service worker v6...');
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
      // Claim all clients immediately (forces update)
      self.clients.claim().then(() => {
        console.log('[SW] Clients claimed - app will use new version');
        // Notify all clients about the update
        return self.clients.matchAll().then(clients => {
          clients.forEach(client => {
            client.postMessage({
              type: 'SW_UPDATED',
              message: 'App updated to latest version!'
            });
          });
        });
      })
    ])
  );
});

// Enhanced fetch handler with better update strategy
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  const request = event.request;
  
  // Skip non-GET requests
  if (request.method !== 'GET') {
    return;
  }
  
  // Handle the main form page with cache-then-network strategy
  if (url.pathname === '/' || url.pathname === '') {
    console.log('[SW] Handling request for main form page');
    event.respondWith(
      // Try cache first for immediate response
      caches.match(request)
        .then(cachedResponse => {
          // Start network request in parallel
          const networkRequest = fetch(request, { cache: 'no-cache' })
            .then(response => {
              if (response.status === 200) {
                const responseClone = response.clone();
                caches.open(CACHE_NAME)
                  .then(cache => {
                    cache.put(request, responseClone);
                    console.log('[SW] Updated cache with fresh main page');
                    
                    // Notify client if content changed
                    if (cachedResponse) {
                      notifyClientOfUpdate();
                    }
                  });
              }
              return response;
            })
            .catch(() => null);
          
          // Return cached version immediately, update in background
          if (cachedResponse) {
            networkRequest; // Update in background
            return cachedResponse;
          }
          
          // No cache, wait for network
          return networkRequest || caches.match('/offline.html');
        })
    );
    return;
  }
  
  // Handle other HTML pages with network-first for fresh content
  if (request.destination === 'document' || 
      request.headers.get('accept')?.includes('text/html') ||
      url.pathname.includes('.html')) {
    
    event.respondWith(
      fetch(request)
        .then(response => {
          if (response.status === 200) {
            const responseClone = response.clone();
            caches.open(CACHE_NAME)
              .then(cache => cache.put(request, responseClone));
          }
          return response;
        })
        .catch(() => {
          return caches.match(request)
            .then(cachedResponse => {
              return cachedResponse || caches.match('/offline.html');
            });
        })
    );
  }
  
  // Handle static assets with cache-first strategy
  else {
    event.respondWith(
      caches.match(request)
        .then(cachedResponse => {
          if (cachedResponse) {
            // Update cache in background
            fetch(request)
              .then(response => {
                if (response.status === 200) {
                  const responseClone = response.clone();
                  caches.open(CACHE_NAME)
                    .then(cache => cache.put(request, responseClone));
                }
              })
              .catch(() => {});
            
            return cachedResponse;
          }
          
          return fetch(request)
            .then(response => {
              if (response.status === 200) {
                const responseClone = response.clone();
                caches.open(CACHE_NAME)
                  .then(cache => cache.put(request, responseClone));
              }
              return response;
            });
        })
    );
  }
});

// Auto-update check function
async function checkForUpdates() {
  try {
    const response = await fetch('/sw.js', { cache: 'no-cache' });
    if (response.ok) {
      const newSWText = await response.text();
      const currentSWText = await self.importScripts ? '' : 'different'; // Simple check
      
      if (newSWText !== currentSWText) {
        console.log('[SW] New version detected, updating...');
        // This will trigger the install event for the new service worker
        self.registration.update();
      }
    }
  } catch (error) {
    console.log('[SW] Update check failed:', error);
  }
}

// Periodic update checks
setInterval(checkForUpdates, UPDATE_CHECK_INTERVAL);

// Background sync for offline form submissions
self.addEventListener('sync', event => {
  if (event.tag === 'submit-reports') {
    event.waitUntil(submitSavedReports());
  }
});

async function submitSavedReports() {
  console.log('[SW] Background sync: submitting saved reports');
  
  try {
    const clients = await self.clients.matchAll();
    if (clients.length > 0) {
      clients[0].postMessage({
        type: 'BACKGROUND_SYNC',
        action: 'SUBMIT_REPORTS'
      });
    }
  } catch (error) {
    console.log('[SW] Background sync error:', error);
  }
}

// Message handling for client communication
self.addEventListener('message', event => {
  console.log('[SW] Received message:', event.data);
  
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
  
  if (event.data && event.data.type === 'CHECK_UPDATE') {
    checkForUpdates();
  }
  
  if (event.data && event.data.type === 'GET_VERSION') {
    event.ports[0].postMessage({
      type: 'VERSION_INFO',
      version: CACHE_NAME,
      updateAvailable: false
    });
  }
});

// Notify clients of updates
async function notifyClientOfUpdate() {
  const clients = await self.clients.matchAll();
  clients.forEach(client => {
    client.postMessage({
      type: 'CONTENT_UPDATED',
      message: 'New content available! Refresh to see the latest version.'
    });
  });
}

// Enhanced push notification handling
self.addEventListener('push', event => {
  if (event.data) {
    const data = event.data.json();
    const options = {
      body: data.body || 'You have a new notification',
      icon: 'https://storage.googleapis.com/smt-misc/SMT-logo.png',
      badge: 'https://storage.googleapis.com/smt-misc/SMT-logo.png',
      vibrate: [100, 50, 100],
      data: {
        dateOfArrival: Date.now(),
        primaryKey: data.primaryKey || '1',
        url: data.url || '/'
      },
      actions: [
        {
          action: 'open',
          title: 'Abrir App',
          icon: 'https://storage.googleapis.com/smt-misc/SMT-logo.png'
        },
        {
          action: 'close',
          title: 'Cerrar'
        }
      ],
      requireInteraction: true,
      tag: 'smt-notification'
    };
    
    event.waitUntil(
      self.registration.showNotification(data.title || 'SMT SecApp', options)
    );
  }
});

// Handle notification clicks
self.addEventListener('notificationclick', event => {
  event.notification.close();
  
  if (event.action === 'open' || !event.action) {
    const urlToOpen = event.notification.data?.url || '/';
    
    event.waitUntil(
      clients.matchAll({
        type: 'window',
        includeUncontrolled: true
      }).then(clientList => {
        // Try to focus existing window
        for (const client of clientList) {
          if (client.url === urlToOpen && 'focus' in client) {
            return client.focus();
          }
        }
        
        // Open new window
        if (clients.openWindow) {
          return clients.openWindow(urlToOpen);
        }
      })
    );
  }
});

// Periodic cache cleanup
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

// Force update function for critical updates
self.addEventListener('fetch', event => {
  // Check for force update flag in requests
  if (event.request.url.includes('?force-update=true')) {
    event.respondWith(
      fetch(event.request).then(response => {
        // Clear all caches and force reload
        caches.keys().then(cacheNames => {
          cacheNames.forEach(cacheName => {
            caches.delete(cacheName);
          });
        });
        
        return response;
      })
    );
  }
});

// Version tracking
const VERSION_INFO = {
  version: CACHE_NAME,
  buildDate: new Date().toISOString(),
  features: [
    'Auto-update support',
    'Background sync',
    'Push notifications',
    'Improved caching',
    'Better offline support'
  ]
};

console.log('[SW] Service Worker v6 loaded with features:', VERSION_INFO.features);