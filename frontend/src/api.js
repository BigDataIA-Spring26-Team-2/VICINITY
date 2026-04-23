const API = import.meta.env.VITE_API_URL || ''

function getToken() {
  return localStorage.getItem('vicinity_token')
}

async function request(path, options = {}) {
  const token = getToken()
  const headers = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...options.headers,
  }

  const res = await fetch(`${API}${path}`, { ...options, headers })
  const data = await res.json()

  if (!res.ok) {
    throw new Error(data.detail || data.error || `HTTP ${res.status}`)
  }

  return data
}

// ─── Listings ────────────────────────────────────────────────

export async function searchListings(params = {}) {
  const qs = new URLSearchParams(
    Object.fromEntries(Object.entries(params).filter(([, v]) => v != null))
  ).toString()
  return request(`/listings/search?${qs}`)
}

export async function getListingDetail(listingId) {
  return request(`/listings/${listingId}`)
}

export async function compareListings(ids) {
  const qs = ids.map(id => `ids=${id}`).join('&')
  return request(`/listings/compare?${qs}`)
}

export async function getScorecard(listingId, params = {}) {
  const qs = new URLSearchParams(params).toString()
  return request(`/listings/${listingId}/scorecard?${qs}`)
}

// ─── Safety ──────────────────────────────────────────────────

export async function getCrimes(lat, lon, params = {}) {
  const qs = new URLSearchParams({ lat, lon, ...params }).toString()
  return request(`/safety/crimes?${qs}`)
}

export async function getCrimesHourly(lat, lon, params = {}) {
  const qs = new URLSearchParams({ lat, lon, ...params }).toString()
  return request(`/safety/crimes/hourly?${qs}`)
}

export async function getNeighborhoodStats(neighborhood, params = {}) {
  const qs = new URLSearchParams({ neighborhood, ...params }).toString()
  return request(`/safety/neighborhood?${qs}`)
}

export async function getComplaints(lat, lon, params = {}) {
  const qs = new URLSearchParams({ lat, lon, ...params }).toString()
  return request(`/safety/complaints?${qs}`)
}

export async function getComplaintsSummary(lat, lon, params = {}) {
  const qs = new URLSearchParams({ lat, lon, ...params }).toString()
  return request(`/safety/complaints/summary?${qs}`)
}

// ─── Map data ────────────────────────────────────────────────

export async function getMapListings(params = {}) {
  const qs = new URLSearchParams(params).toString()
  return request(`/map/listings?${qs}`)
}

export async function getMapTransit(params = {}) {
  const qs = new URLSearchParams(params).toString()
  return request(`/map/transit?${qs}`)
}

export async function getMapAmenities(lat, lon, params = {}) {
  const qs = new URLSearchParams({ lat, lon, ...params }).toString()
  return request(`/map/amenities?${qs}`)
}

export async function getMapRoutes(params = {}) {
  const qs = new URLSearchParams(params).toString()
  return request(`/map/routes?${qs}`)
}

// ─── Scorecards ──────────────────────────────────────────────

export async function getRouteScorecard(routeId, params = {}) {
  const qs = new URLSearchParams(params).toString()
  return request(`/scorecards/route/${routeId}?${qs}`)
}

// ─── Users ───────────────────────────────────────────────────

export async function getUserProfile() {
  return request('/users/profile')
}

export async function getUserBookmarks() {
  return request('/users/bookmarks')
}

export async function getUserRoutes(params = {}) {
  const qs = new URLSearchParams(params).toString()
  return request(`/users/routes?${qs}`)
}