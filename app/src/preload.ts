// Two small APIs, each only where it belongs. The start screen (a local file) gets agentIDEStart: its state, uv's
// progress lines, choose a project, try again, repair Python. The workspace page (served by the server over http) gets
// window.agentIDE.chooseFolder(): Electron's own folder dialog, which the page uses in place of the server's osascript
// chooser when it finds it; and window.agentIDE.miniMap, the name of the mini-map's window. The main process checks the sender's address on every call as well.
import { contextBridge, ipcRenderer } from 'electron';

// the mini-map's window name: the page opens it with window.open('', name) in place of Document Picture-in-Picture, which
// Electron does not provide (logic.ts MINIMAP_FRAME; a sandboxed preload cannot import logic.ts, so the name is repeated)
const MINIMAP_FRAME = 'topographic-minimap';

if (location.protocol === 'file:') {
  contextBridge.exposeInMainWorld('agentIDEStart', {
    screen: () => ipcRenderer.invoke('start:screen'),
    choose: () => ipcRenderer.invoke('start:choose'),
    retry: () => ipcRenderer.invoke('start:retry'),
    repair: () => ipcRenderer.invoke('start:repair'),
    titleDoubleClick: () => ipcRenderer.invoke('app:title-double-click'),          // its title band (logic.ts titleDoubleClick)
    onScreen: (fn: (s: unknown) => void) => { ipcRenderer.on('start:screen', (_e, s) => fn(s)); },
    onProgress: (fn: (line: string) => void) => { ipcRenderer.on('start:progress', (_e, l) => fn(String(l))); },
  });
} else if (location.protocol === 'http:' && location.hostname === '127.0.0.1') {
  contextBridge.exposeInMainWorld('agentIDE', {
    app: true,
    miniMap: MINIMAP_FRAME,
    chooseFolder: (): Promise<{ path?: string; cancelled?: boolean }> => ipcRenderer.invoke('app:choose-folder'),
    // the title band (live.html "the app's title band"): its double-click, and full screen, when the band and offsets go
    titleDoubleClick: () => ipcRenderer.invoke('app:title-double-click'),
    fullScreen: (): Promise<boolean> => ipcRenderer.invoke('app:full-screen'),
    onFullScreen: (fn: (on: boolean) => void) => { ipcRenderer.on('app:full-screen', (_e, on) => fn(!!on)); },
  });
}
