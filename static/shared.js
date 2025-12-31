// Theme management
function initTheme() {
    const saved = localStorage.getItem('theme') || 'light';
    document.documentElement.setAttribute('data-theme', saved);
}

function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme');
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
}

initTheme();

// Stats loading for navbar badges
async function loadNavStats() {
    try {
        const res = await fetch('/api/stats');
        const stats = await res.json();

        const unseenBadge = document.getElementById('navUnseenBadge');
        if (unseenBadge) {
            unseenBadge.textContent = stats.unseen_items || 0;
            unseenBadge.style.display = stats.unseen_items > 0 ? 'block' : 'none';
        }
    } catch (e) {
        console.error('Failed to load nav stats:', e);
    }
}

loadNavStats();