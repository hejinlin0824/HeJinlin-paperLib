import { createRouter, createWebHistory } from 'vue-router'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: () => import('./pages/Dashboard.vue') },
    { path: '/library', name: 'library', component: () => import('./pages/Library.vue') },
    { path: '/reader/:id', name: 'reader', component: () => import('./pages/Reader.vue') },
    { path: '/log', name: 'log', component: () => import('./pages/LogTable.vue') },
    { path: '/settings', name: 'settings', component: () => import('./pages/Settings.vue') },
  ],
})
