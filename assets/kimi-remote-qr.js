/*
 * KimiRemoteQR — QR→SVG bridge over the vendored Nayuki qrcodegen
 * library (assets/vendor/qrcodegen.js, MIT License, Project Nayuki).
 *
 * window.KimiRemoteQR.toSVG(text) -> inline <svg> string.
 * Fixed rendering: 4-module quiet zone, 240px display size,
 * Ecc.MEDIUM, crispEdges, white background, black modules.
 * The payload text is never embedded into the SVG.
 * Oversize payloads propagate qrcodegen's RangeError("Data too long").
 *
 * Load assets/vendor/qrcodegen.js first. Works in plain <script> and
 * node vm (qrcodegen is resolved lazily through the global object).
 */
(function(root) {
  'use strict';

  var BORDER = 4;
  var SCALE = 240;

  function getQrGen() {
    var g = (typeof globalThis === 'object' && globalThis)
      || root
      || (typeof window === 'object' && window)
      || (typeof self === 'object' && self)
      || null;
    if (g && g.qrcodegen && g.qrcodegen.QrCode)
      return g.qrcodegen;
    throw new Error('qrcodegen is not loaded (load assets/vendor/qrcodegen.js first)');
  }

  function toSVG(text) {
    if (text == null)
      throw new TypeError('KimiRemoteQR.toSVG: text is required');
    var qg = getQrGen();
    var qr = qg.QrCode.encodeText(String(text), qg.QrCode.Ecc.MEDIUM);
    var dim = qr.size + BORDER * 2;
    var parts = [];
    for (var y = 0; y < qr.size; y++)
      for (var x = 0; x < qr.size; x++)
        if (qr.getModule(x, y))
          parts.push('M' + x + ',' + y + 'h1v1h-1z');
    return '<svg xmlns="http://www.w3.org/2000/svg" version="1.1"'
      + ' viewBox="0 0 ' + dim + ' ' + dim + '"'
      + ' width="' + SCALE + '" height="' + SCALE + '"'
      + ' shape-rendering="crispEdges">'
      + '<rect width="100%" height="100%" fill="#FFFFFF"/>'
      + '<path d="' + parts.join('') + '" fill="#000000"'
      + ' transform="translate(' + BORDER + ',' + BORDER + ')"/>'
      + '</svg>';
  }

  var api = { toSVG: toSVG };
  var g = (typeof globalThis === 'object' && globalThis)
    || root
    || (typeof window === 'object' && window);
  if (g) g.KimiRemoteQR = api;
  if (typeof module === 'object' && module && module.exports)
    module.exports = api;
})(typeof window === 'object' ? window : (typeof self === 'object' ? self : this));
