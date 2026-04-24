const API = import.meta.env.VITE_API_URL || ''

function tok() {
  return localStorage.getItem('vicinity_token')
}

async function req(path, options = {}) {
  const token = tok()
  const headers = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(options.headers || {}),
  }
  const res = await fetch(`${API}${path}`, { ...options, headers })
  const text = await res.text()
  let data
  try { data = text ? JSON.parse(text) : {} } catch { data = { _raw: text } }
  if (!res.ok) {
    const msg = data?.detail || data?.error || `HTTP ${res.status}`
    const e = new Error(msg)
    e.status = res.status
    e.body = data
    throw e
  }
  return data
}

const qs = (params) => new URLSearchParams(
  Object.fromEntries(Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== ''))
).toString()

// Listings
export const searchListings        = (p = {})       => req(`/listings/search?${qs(p)}`)
export const getListingDetail      = (id)           => req(`/listings/${id}`)
export const getListingNarratives  = (id, p = {})   => req(`/listings/${id}/narratives?${qs(p)}`)
export const compareListings       = (ids)          => req(`/listings/compare?${ids.map(i => `ids=${i}`).join('&')}`)
export const getScorecard          = (id, p = {})   => req(`/listings/${id}/scorecard?${qs(p)}`)

// Safety
export const getCrimes             = (lat, lon, p = {}) => req(`/safety/crimes?${qs({ lat, lon, ...p })}`)
export const getCrimesHeatmap      = (p = {})           => req(`/safety/crimes/heatmap?${qs(p)}`)
export const getCrimesDistribution = (lat, lon, p = {}) => req(`/safety/crimes/distribution?${qs({ lat, lon, ...p })}`)
export const getCrimesTypes        = (lat, lon, p = {}) => req(`/safety/crimes/types?${qs({ lat, lon, ...p })}`)
export const getCrimesHourly       = (lat, lon, p = {}) => req(`/safety/crimes/hourly?${qs({ lat, lon, ...p })}`)
export const getComplaints         = (lat, lon, p = {}) => req(`/safety/complaints?${qs({ lat, lon, ...p })}`)
export const getComplaintsSummary  = (lat, lon, p = {}) => req(`/safety/complaints/summary?${qs({ lat, lon, ...p })}`)

// Map
export const getMapListings        = (p = {})           => req(`/map/listings?${qs(p)}`)
export const getMapTransit         = (p = {})           => req(`/map/transit?${qs(p)}`)
export const getMapAmenities       = (lat, lon, p = {}) => req(`/map/amenities?${qs({ lat, lon, ...p })}`)

// User
export const getUserBookmarks      = ()         => req('/users/bookmarks')
export const getUserRoutes         = (p = {})   => req(`/users/routes?${qs(p)}`)