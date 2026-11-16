/** Saves `data` as a pretty-printed JSON file named `filename` through a temporary anchor. */
export function downloadJson(filename: string, data: unknown): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // After the click has been handled: revoking within it can cancel the download in some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
