// Settings read from frontend/.env.local (see .env.example). Vite exposes only VITE_* variables.

export type DataSource = 'mock' | 'live'

export const DATA_SOURCE: DataSource = import.meta.env.VITE_DATA_SOURCE === 'live' ? 'live' : 'mock'

export const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000'
