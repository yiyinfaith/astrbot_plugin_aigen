# Bundled frontend dependencies

- `vue.global.prod.js`: Vue 3.5.22 global production distribution, https://unpkg.com/vue@3.5.22/dist/vue.global.prod.js. Copyright Vue contributors; MIT license. https://github.com/vuejs/core/blob/v3.5.22/LICENSE
- `liquid-glass.js`: GooseHyperGlassCDN2.5, exact script distributed at https://glass.goose.gs.cn/liquid-glass.js. Usage documentation: https://glass.goose.cc.cd/doc/. Upstream projects credited by the documentation: https://github.com/Kyant0/AndroidLiquidGlass, https://github.com/martin65536/liquid-glass-webgl, and https://github.com/aaaa-zhen/siri-glsl (Siri animation).

The original distributions are preserved. Integration and lifecycle handling live in `../glass.js`; AstrBot rewrites local relative asset paths for its sandboxed plugin Page.
