import { beforeAll, expect } from "vitest";
import "@testing-library/jest-dom/vitest";

beforeAll(() => {
  process.env.TZ = "UTC";
});

// jsdom shims for Radix, cmdk and react-resizable-panels. Each is installed only when jsdom lacks it,
// and stays writable so a test can still replace it (vi.stubGlobal, Object.defineProperty).
function define(target: object, key: string, value: unknown) {
  if (key in target) return;
  Object.defineProperty(target, key, { value, writable: true, configurable: true });
}

class ResizeObserverShim {
  observe() {}
  unobserve() {}
  disconnect() {}
}
define(globalThis, "ResizeObserver", ResizeObserverShim);

if (typeof window !== "undefined") {
  class DOMRectShim {
    x: number;
    y: number;
    width: number;
    height: number;
    constructor(x = 0, y = 0, width = 0, height = 0) {
      this.x = x;
      this.y = y;
      this.width = width;
      this.height = height;
    }
    get top() { return this.y; }
    get left() { return this.x; }
    get right() { return this.x + this.width; }
    get bottom() { return this.y + this.height; }
    toJSON() { return { x: this.x, y: this.y, width: this.width, height: this.height, top: this.top, left: this.left, right: this.right, bottom: this.bottom }; }
    static fromRect(rect: { x?: number; y?: number; width?: number; height?: number } = {}) {
      return new DOMRectShim(rect.x, rect.y, rect.width, rect.height);
    }
  }
  define(window, "DOMRect", DOMRectShim);

  define(window, "matchMedia", (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }));

  const elementProto = window.Element.prototype;
  define(elementProto, "hasPointerCapture", () => false);
  define(elementProto, "setPointerCapture", () => {});
  define(elementProto, "releasePointerCapture", () => {});
  define(elementProto, "scrollIntoView", () => {});
}
