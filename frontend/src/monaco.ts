// Bundle Monaco locally (no CDN) and wire up its language workers for Vite.
import { loader } from '@monaco-editor/react'
import * as monaco from 'monaco-editor'
import EditorWorker from 'monaco-editor/editor/editor.worker?worker'
import CssWorker from 'monaco-editor/language/css/css.worker?worker'
import HtmlWorker from 'monaco-editor/language/html/html.worker?worker'
import JsonWorker from 'monaco-editor/language/json/json.worker?worker'
import TsWorker from 'monaco-editor/language/typescript/ts.worker?worker'

self.MonacoEnvironment = {
  getWorker(_id: string, label: string) {
    if (label === 'json') return new JsonWorker()
    if (label === 'css' || label === 'scss' || label === 'less') return new CssWorker()
    if (label === 'html' || label === 'handlebars' || label === 'razor') return new HtmlWorker()
    if (label === 'typescript' || label === 'javascript') return new TsWorker()
    return new EditorWorker()
  },
}

// We only view/edit files; type-checking them without their dependencies would just show noise.
monaco.typescript.typescriptDefaults.setDiagnosticsOptions({ noSemanticValidation: true, noSyntaxValidation: false })
monaco.typescript.javascriptDefaults.setDiagnosticsOptions({ noSemanticValidation: true, noSyntaxValidation: false })

// Editor themes matched to the app's surfaces (see index.css tokens).
monaco.editor.defineTheme('pa-dark', {
  base: 'vs-dark',
  inherit: true,
  rules: [],
  colors: {
    'editor.background': '#111214',
    'editor.lineHighlightBackground': '#17181b',
    'editor.lineHighlightBorder': '#00000000',
    'editorLineNumber.foreground': '#4a4d53',
    'editorLineNumber.activeForeground': '#a1a3a9',
    'editorGutter.background': '#111214',
    'editorIndentGuide.background1': '#1f2124',
    'editorWidget.background': '#17181b',
    'editorWidget.border': '#303338',
    'editorHoverWidget.background': '#17181b',
    'editorHoverWidget.border': '#303338',
    'minimap.background': '#111214',
    'scrollbarSlider.background': '#30333866',
    'diffEditor.insertedLineBackground': '#3fb97f0d',
    'diffEditor.removedLineBackground': '#ef5b5b0d',
    'diffEditor.insertedTextBackground': '#3fb97f1f',
    'diffEditor.removedTextBackground': '#ef5b5b1f',
  },
})
monaco.editor.defineTheme('pa-light', {
  base: 'vs',
  inherit: true,
  rules: [],
  colors: {
    'editor.background': '#ffffff',
    'editor.lineHighlightBackground': '#f6f6f7',
    'editor.lineHighlightBorder': '#00000000',
    'editorLineNumber.foreground': '#b4b6bc',
    'editorLineNumber.activeForeground': '#55575e',
    'editorIndentGuide.background1': '#ebebee',
    'diffEditor.insertedLineBackground': '#1a7f4b10',
    'diffEditor.removedLineBackground': '#c9343410',
    'diffEditor.insertedTextBackground': '#1a7f4b26',
    'diffEditor.removedTextBackground': '#c9343426',
  },
})

loader.config({ monaco })

// Monaco measures glyph widths once; re-measure after the bundled mono font has loaded.
document.fonts?.ready.then(() => monaco.editor.remeasureFonts())

export const prefersDark = () => window.matchMedia('(prefers-color-scheme: dark)').matches
export const monacoTheme = () => (prefersDark() ? 'pa-dark' : 'pa-light')
export const EDITOR_FONT = {
  fontFamily: "'Geist Mono Variable', ui-monospace, Consolas, monospace",
  fontSize: 12.5,
  lineHeight: 20,
  fontLigatures: false,
}
