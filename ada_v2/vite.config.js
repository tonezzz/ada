import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

function denyDotGit() {
    return {
        name: 'deny-dot-git',
        configureServer(server) {
            server.middlewares.use((req, res, next) => {
                const url = req.url || ''
                if (url.startsWith('/.git/') || url === '/.git' || url.startsWith('/.git?')) {
                    res.statusCode = 403
                    res.end('Forbidden')
                    return
                }
                next()
            })
        },
    }
}

// https://vitejs.dev/config/
export default defineConfig({
    plugins: [denyDotGit(), react()],
    base: './', // Important for Electron
    server: {
        port: 5173,
        allowedHosts: ['idc1.vpn', 'ada.idc1.surf-thailand.com'],
        fs: {
            deny: ['**/.git/**'],
        },
    }
})
