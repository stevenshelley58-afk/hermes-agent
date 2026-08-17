import { describe, expect, it } from 'vitest'

import {
  isMessagingSource,
  LOCAL_SESSION_SOURCE_IDS,
  MESSAGING_SESSION_SOURCE_IDS,
  sessionSourceSearchTerms
} from './session-source'

// API-server sessions are local gateway clients such as Frank. They belong in
// generic Recents: classifying them as messaging excludes them from Recents,
// while Desktop has no API platform section in which to render them.
describe('API server session registration', () => {
  it('keeps API sessions in local recents instead of a hidden messaging section', () => {
    expect(LOCAL_SESSION_SOURCE_IDS).toContain('api_server')
    expect(MESSAGING_SESSION_SOURCE_IDS).not.toContain('api_server')
    expect(isMessagingSource('api_server')).toBe(false)
  })

  it('keeps API sessions findable by their source label', () => {
    expect(sessionSourceSearchTerms('api_server')).toContain('API')
  })
})

// Regression guard for #46761 / PR #47395: Photon (iMessage) must keep its own
// sidebar section. refreshMessagingSessions() filters rows through
// isMessagingSource(), so this entry is the sole condition that keeps Photon
// sessions out of generic recents. A silent removal would regress the feature
// with no test failure — these asserts pin the contract.
describe('photon messaging source registration', () => {
  it('treats photon as a messaging source (own sidebar section)', () => {
    expect(isMessagingSource('photon')).toBe(true)
  })

  it('is case/space insensitive on the source id', () => {
    expect(isMessagingSource('PHOTON')).toBe(true)
    expect(isMessagingSource('  photon ')).toBe(true)
  })

  it('exposes the iMessage/messages search aliases so Photon sessions are findable', () => {
    const terms = sessionSourceSearchTerms('photon')
    expect(terms).toContain('imessage')
    expect(terms).toContain('messages')
  })

  it('is registered in the messaging source id list', () => {
    expect(MESSAGING_SESSION_SOURCE_IDS).toContain('photon')
  })

  it('does not flag local/CLI-ish sources as messaging (guard sanity)', () => {
    expect(isMessagingSource('cli')).toBe(false)
    expect(isMessagingSource(null)).toBe(false)
    expect(isMessagingSource(undefined)).toBe(false)
  })
})
