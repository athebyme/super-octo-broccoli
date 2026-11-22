# My Products inline import bundle

The source package pins Vue 3.5.46, Vite 8.3.8, and `@vitejs/plugin-vue` 6.0.11 in `package.json` and `package-lock.json`. Build with Node 22.22.2 and npm 10.9.7:

```sh
./frontend/build.sh
```

The Vue runtime is copied from the official Vue npm package into `static/vendor/vue-3.5.46.global.prod.js`. The IIFE bundle externalizes `vue` to the page's existing `window.Vue`; it contains only the import component and shared stylesheet. The build writes `static/workspace/my-products-import.iife.js` and `static/workspace/my-products-import.css` without clearing other workspace artifacts.
