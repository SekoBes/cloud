// Cloudflare Worker - TV-2
// Token gömülü - 2026-02-19 23:33:09

const GITHUB_USER = 'SekoBes';
const GITHUB_REPO = 'S5';
const GITHUB_BRANCH = 'main';
const GITHUB_TOKEN = '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';  // Token direkt burada

async function handleRequest(request) {
  const url = new URL(request.url);
  const path = url.pathname;
  
  if (path === '/playlist.m3u') {
    return await servePlaylist();
  }
  
  if (path.endsWith('.m3u8')) {
    const fileName = path.substring(1);
    return await serveM3U8(fileName);
  }
  
  return new Response('TV-2 Proxy', { status: 404 });
}

async function servePlaylist() {
  try {
    const githubUrl = `https://api.github.com/repos/${GITHUB_USER}/${GITHUB_REPO}/contents/TV?ref=${GITHUB_BRANCH}`;
    
    const response = await fetch(githubUrl, {
      headers: {
        'Authorization': `token ${GITHUB_TOKEN}`,
        'User-Agent': 'Cloudflare-Worker'
      }
    });
    
    if (!response.ok) {
      return new Response('GitHub hatası', { status: 500 });
    }
    
    const files = await response.json();
    let playlist = '#EXTM3U\n#EXTINF:-1,TV-2 Kanal Listesi\n';
    
    for (const file of files) {
      if (file.name.endsWith('.m3u8')) {
        const kanalAdi = file.name.replace('.m3u8', '').replace(/_/g, ' ');
        playlist += `#EXTINF:-1 tvg-id="${kanalAdi}" tvg-name="${kanalAdi}",${kanalAdi}\n`;
        playlist += `https://tv-2.sekobes.workers.dev/${file.name}\n`;
      }
    }
    
    return new Response(playlist, {
      headers: { 'Content-Type': 'application/vnd.apple.mpegurl' }
    });
    
  } catch (error) {
    return new Response(`Hata: ${error.message}`, { status: 500 });
  }
}

async function serveM3U8(fileName) {
  try {
    const githubUrl = `https://api.github.com/repos/${GITHUB_USER}/${GITHUB_REPO}/contents/TV/${fileName}?ref=${GITHUB_BRANCH}`;
    
    const response = await fetch(githubUrl, {
      headers: {
        'Authorization': `token ${GITHUB_TOKEN}`,
        'User-Agent': 'Cloudflare-Worker'
      }
    });
    
    if (!response.ok) {
      return new Response('Dosya bulunamadı', { status: 404 });
    }
    
    const data = await response.json();
    const content = atob(data.content.replace(/\n/g, ''));
    
    return new Response(content, {
      headers: { 'Content-Type': 'application/vnd.apple.mpegurl' }
    });
    
  } catch (error) {
    return new Response(`Hata: ${error.message}`, { status: 500 });
  }
}

addEventListener('fetch', event => {
  event.respondWith(handleRequest(event.request));
});
