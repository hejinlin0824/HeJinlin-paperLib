import { createRouter, createWebHistory } from 'vue-router'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: () => import('./pages/Dashboard.vue') },
    { path: '/library', name: 'library', component: () => import('./pages/Library.vue') },
    { path: '/reader/:id', name: 'reader', component: () => import('./pages/Reader.vue') },
    { path: '/log', name: 'log', component: () => import('./pages/LogTable.vue') },
    { path: '/graph', name: 'graph', component: () => import('./pages/KnowledgeGraph.vue') },
    { path: '/kg-understanding', name: 'kg-understanding', component: () => import('./pages/PaperUnderstanding.vue') },
    { path: '/kg-intelligence', name: 'kg-intelligence', component: () => import('./pages/PaperIntelligence.vue') },
    { path: '/settings', name: 'settings', component: () => import('./pages/Settings.vue') },
  ],
})
