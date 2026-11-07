/* Findling add-on for the pdf.js viewer (pdfjs/web/viewer.html loads it):
   highlights a search result's chunk on its page and scrolls to it.

   viewer.html?file=/files/…&folder=…&path=…&chunk=N&q=…#page=P
*/
(function () {
  "use strict";
  const params = new URLSearchParams(location.search);
  const folder = params.get("folder"), path = params.get("path");
  if (!folder || !path) return;
  const query = new URLSearchParams({ folder, path, chunk: params.get("chunk") ?? "", q: params.get("q") ?? "" });
  const dataReady = fetch("/api/chunk?" + query)
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null);

  function whenReady(callback) {
    const app = window.PDFViewerApplication;
    if (app && app.initializedPromise) app.initializedPromise.then(() => callback(app));
    else setTimeout(() => whenReady(callback), 50);
  }

  whenReady(async (app) => {
    const data = await dataReady;
    if (!data || (!data.text && !data.needles.length)) return;
    const H = window.FindlingHighlight;
    let scrolled = false;

    function apply(pageNumber) {
      const layer = document.querySelector(`.page[data-page-number="${pageNumber}"] .textLayer`);
      if (!layer || layer.querySelector("mark[class^=findling]")) return;
      if (data.page ? pageNumber !== data.page : !H.locate(H.textNodes(layer).text, data.text)) return;
      const first = H.highlightDom(layer, data.text, data.needles);
      if (first && !scrolled) {
        scrolled = true;
        H.scrollTo(first);
      }
    }

    app.eventBus.on("textlayerrendered", (e) => apply(e.pageNumber));
    // Pages whose text was already rendered while the chunk was loading.
    document.querySelectorAll(".page[data-page-number]").forEach((p) => apply(+p.dataset.pageNumber));
  });
})();
