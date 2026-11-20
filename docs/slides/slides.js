(function () {
  'use strict';
  var W = 1920, H = 1080;
  var deck = document.getElementById('deck');
  var slides = Array.prototype.slice.call(deck.querySelectorAll(':scope > section'));
  var count = document.getElementById('count');
  var notes = document.getElementById('notes');
  var notesText = document.getElementById('notesText');
  var notesBtn = document.getElementById('notesBtn');
  var cur = -1;

  function fit() {
    var s = Math.min(window.innerWidth / W, window.innerHeight / H);
    deck.style.setProperty('--scale', String(s));
  }

  function fromHash() {
    var n = parseInt((location.hash || '').replace('#', ''), 10);
    return isNaN(n) ? 0 : Math.min(Math.max(n, 1), slides.length) - 1;
  }

  function show(i, push) {
    i = Math.min(Math.max(i, 0), slides.length - 1);
    if (i === cur) return;
    if (cur >= 0) slides[cur].classList.remove('active');
    cur = i;
    slides[cur].classList.add('active');
    for (var k = 0; k < slides.length; k++) slides[k].setAttribute('aria-hidden', k === cur ? 'false' : 'true');
    count.textContent = (cur + 1) + ' / ' + slides.length;
    var aside = slides[cur].querySelector('aside');
    notesText.textContent = aside ? aside.textContent : '';
    var h = '#' + (cur + 1);
    if (location.hash !== h) {
      if (push) history.pushState(null, '', h); else history.replaceState(null, '', h);
    }
  }

  function go(d) { show(cur + d, true); }

  function toggleNotes() {
    notes.hidden = !notes.hidden;
    notesBtn.setAttribute('aria-pressed', String(!notes.hidden));
  }

  document.addEventListener('keydown', function (e) {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    switch (e.key) {
      case 'ArrowRight': case 'ArrowDown': case 'PageDown': case ' ': case 'Spacebar':
        go(e.shiftKey && e.key === ' ' ? -1 : 1); break;
      case 'ArrowLeft': case 'ArrowUp': case 'PageUp': go(-1); break;
      case 'Home': show(0, true); break;
      case 'End': show(slides.length - 1, true); break;
      case 'n': case 'N': toggleNotes(); break;
      default: return;
    }
    e.preventDefault();
  });

  document.querySelector('.viewport').addEventListener('click', function (e) {
    if (e.target.closest('a')) return;
    go(e.clientX < window.innerWidth / 2 ? -1 : 1);
  });
  document.getElementById('prev').addEventListener('click', function () { go(-1); });
  document.getElementById('next').addEventListener('click', function () { go(1); });
  notesBtn.addEventListener('click', toggleNotes);

  var tx = null, ty = null;
  document.addEventListener('touchstart', function (e) {
    if (e.touches.length !== 1) { tx = null; return; }
    tx = e.touches[0].clientX; ty = e.touches[0].clientY;
  }, { passive: true });
  document.addEventListener('touchend', function (e) {
    if (tx === null) return;
    var dx = e.changedTouches[0].clientX - tx, dy = e.changedTouches[0].clientY - ty;
    tx = null;
    if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy)) go(dx < 0 ? 1 : -1);
  }, { passive: true });

  window.addEventListener('hashchange', function () { show(fromHash(), false); });
  window.addEventListener('resize', fit);
  fit();
  show(fromHash(), false);
})();
