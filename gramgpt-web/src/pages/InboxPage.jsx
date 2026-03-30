import { useEffect, useState, useRef } from 'react'
import { accountsAPI } from '../services/api'
import { Card, Button, Input, Modal, Badge, Spinner, Empty, StatusBadge } from '../components/ui'
import api from '../services/api'

// Заглушки для AI-диалогов, пока бэкенд ai_dialogs не полностью реализован.
// API-вызовы идут через base api instance к /api/v1/inbox/*

const inboxAPI = {
  getDialogs: (accountId) =>
    api.get(`/inbox/accounts/${accountId}/dialogs`).catch(() => ({ data: [] })),

  getMessages: (accountId, contactId) =>
    api.get(`/inbox/accounts/${accountId}/dialogs/${contactId}/messages`).catch(() => ({ data: [] })),

  sendMessage: (accountId, contactId, text) =>
    api.post(`/inbox/accounts/${accountId}/dialogs/${contactId}/send`, { text }),

  setPrompt: (accountId, contactId, prompt, isActive) =>
    api.post(`/inbox/accounts/${accountId}/dialogs/${contactId}/ai-config`, { system_prompt: prompt, is_active: isActive }),

  getAIConfig: (accountId, contactId) =>
    api.get(`/inbox/accounts/${accountId}/dialogs/${contactId}/ai-config`).catch(() => ({ data: { system_prompt: '', is_active: false } })),
}


export default function InboxPage() {
  const [accounts, setAccounts] = useState([])
  const [selectedAccount, setSelectedAccount] = useState(null)
  const [dialogs, setDialogs] = useState([])
  const [selectedDialog, setSelectedDialog] = useState(null)
  const [messages, setMessages] = useState([])
  const [loading, setLoading] = useState(true)
  const [dialogsLoading, setDialogsLoading] = useState(false)
  const [msgsLoading, setMsgsLoading] = useState(false)
  const [sending, setSending] = useState(false)
  const [messageText, setMessageText] = useState('')
  const [aiModal, setAiModal] = useState(false)
  const [aiPrompt, setAiPrompt] = useState('')
  const [aiActive, setAiActive] = useState(false)
  const [aiSaving, setAiSaving] = useState(false)
  const [toast, setToast] = useState(null)
  const msgsEndRef = useRef(null)

  const showToast = (text, type = 'success') => {
    setToast({ text, type })
    setTimeout(() => setToast(null), 3500)
  }

  // Load accounts
  useEffect(() => {
    (async () => {
      try { const { data } = await accountsAPI.list(); setAccounts(data.filter(a => a.status === 'active')) }
      catch {}
      setLoading(false)
    })()
  }, [])

  // Load dialogs when account selected
  const loadDialogs = async (acc) => {
    setSelectedAccount(acc)
    setSelectedDialog(null)
    setMessages([])
    setDialogsLoading(true)
    try { const { data } = await inboxAPI.getDialogs(acc.id); setDialogs(Array.isArray(data) ? data : []) }
    catch { setDialogs([]) }
    setDialogsLoading(false)
  }

  // Load messages for dialog
  const loadMessages = async (dialog) => {
    setSelectedDialog(dialog)
    setMsgsLoading(true)
    try {
      const { data } = await inboxAPI.getMessages(selectedAccount.id, dialog.contact_id || dialog.id)
      setMessages(Array.isArray(data) ? data : [])
    } catch { setMessages([]) }
    setMsgsLoading(false)
  }

  // Scroll to bottom
  useEffect(() => {
    msgsEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Send message manually (operator intervention)
  const handleSend = async () => {
    if (!messageText.trim() || !selectedAccount || !selectedDialog) return
    setSending(true)
    try {
      await inboxAPI.sendMessage(selectedAccount.id, selectedDialog.contact_id || selectedDialog.id, messageText)
      setMessages(prev => [...prev, { from: 'me', text: messageText, time: new Date().toISOString(), is_ai: false }])
      setMessageText('')
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка отправки', 'error') }
    setSending(false)
  }

  // AI config
  const openAIConfig = async () => {
    if (!selectedAccount || !selectedDialog) return
    try {
      const { data } = await inboxAPI.getAIConfig(selectedAccount.id, selectedDialog.contact_id || selectedDialog.id)
      setAiPrompt(data.system_prompt || '')
      setAiActive(data.is_active || false)
    } catch {}
    setAiModal(true)
  }

  const saveAIConfig = async () => {
    setAiSaving(true)
    try {
      await inboxAPI.setPrompt(selectedAccount.id, selectedDialog.contact_id || selectedDialog.id, aiPrompt, aiActive)
      setAiModal(false)
      showToast('ИИ-конфигурация сохранена')
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setAiSaving(false)
  }

  if (loading) return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
      <Spinner size={32} />
    </div>
  )

  return (
    <div style={{ display: 'flex', height: '100%', animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)' }}>
      {/* Toast */}
      {toast && (
        <div style={{
          position: 'fixed', top: 24, right: 24, zIndex: 999,
          padding: '12px 20px', borderRadius: 12, fontSize: 13, fontWeight: 600,
          background: toast.type === 'error' ? 'var(--red-dim)' : 'var(--green-dim)',
          color: toast.type === 'error' ? 'var(--red)' : 'var(--green)',
          border: `1px solid ${toast.type === 'error' ? 'rgba(248,81,73,0.3)' : 'rgba(61,214,140,0.3)'}`,
          boxShadow: '0 8px 30px rgba(0,0,0,0.5)', animation: 'fadeUp 0.3s ease',
        }}>{toast.text}</div>
      )}

      {/* ── Account list (left sidebar) ──────────────────── */}
      <div style={{
        width: 220, flexShrink: 0, borderRight: '1px solid var(--border)',
        display: 'flex', flexDirection: 'column', background: 'var(--bg-2)',
      }}>
        <div style={{ padding: '18px 16px 12px', borderBottom: '1px solid var(--border)' }}>
          <div style={{ fontSize: 11, color: 'var(--pink)', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 4 }}>◆ ВХОДЯЩИЕ</div>
          <div style={{ fontSize: 15, fontWeight: 800, letterSpacing: '-0.03em' }}>Аккаунты</div>
          <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>{accounts.length} активных</div>
        </div>
        <div style={{ flex: 1, overflow: 'auto', padding: '8px 8px' }}>
          {accounts.length === 0 ? (
            <div style={{ padding: 20, textAlign: 'center', fontSize: 12, color: 'var(--text-3)' }}>
              Нет активных аккаунтов
            </div>
          ) : accounts.map(acc => (
            <div key={acc.id} onClick={() => loadDialogs(acc)} style={{
              padding: '10px 12px', borderRadius: 10, cursor: 'pointer', marginBottom: 2,
              background: selectedAccount?.id === acc.id ? 'rgba(124,77,255,0.18)' : 'transparent',
              border: selectedAccount?.id === acc.id ? '1px solid rgba(124,77,255,0.25)' : '1px solid transparent',
              transition: 'all 0.15s',
            }}
            onMouseEnter={e => { if (selectedAccount?.id !== acc.id) e.currentTarget.style.background = 'rgba(255,255,255,0.04)' }}
            onMouseLeave={e => { if (selectedAccount?.id !== acc.id) e.currentTarget.style.background = 'transparent' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <div style={{
                  width: 30, height: 30, borderRadius: 8, flexShrink: 0,
                  background: 'linear-gradient(135deg, rgba(124,77,255,0.3), rgba(255,61,154,0.2))',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 12, fontWeight: 700, color: 'var(--violet)',
                }}>{acc.first_name?.[0]?.toUpperCase() || '?'}</div>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 12, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {acc.first_name || acc.phone}
                  </div>
                  <div style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>{acc.phone}</div>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ── Dialog list (middle) ─────────────────────────── */}
      <div style={{
        width: 280, flexShrink: 0, borderRight: '1px solid var(--border)',
        display: 'flex', flexDirection: 'column',
      }}>
        <div style={{ padding: '18px 16px 12px', borderBottom: '1px solid var(--border)' }}>
          <div style={{ fontSize: 14, fontWeight: 700, letterSpacing: '-0.02em' }}>
            {selectedAccount ? `Диалоги` : 'Выберите аккаунт'}
          </div>
          {selectedAccount && (
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>
              {selectedAccount.first_name || selectedAccount.phone} · {dialogs.length} диалогов
            </div>
          )}
        </div>
        <div style={{ flex: 1, overflow: 'auto' }}>
          {!selectedAccount ? (
            <div style={{ padding: 40, textAlign: 'center', fontSize: 12, color: 'var(--text-3)' }}>
              ← Выберите аккаунт слева
            </div>
          ) : dialogsLoading ? (
            <div style={{ display: 'flex', justifyContent: 'center', padding: 40 }}><Spinner size={20} /></div>
          ) : dialogs.length === 0 ? (
            <div style={{ padding: 40, textAlign: 'center' }}>
              <div style={{ fontSize: 32, marginBottom: 8 }}>💬</div>
              <div style={{ fontSize: 13, fontWeight: 600 }}>Нет диалогов</div>
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>
                Входящие сообщения появятся здесь
              </div>
            </div>
          ) : dialogs.map((d, i) => (
            <div key={d.contact_id || d.id || i} onClick={() => loadMessages(d)} style={{
              padding: '12px 16px', cursor: 'pointer',
              borderBottom: '1px solid var(--border)',
              background: (selectedDialog?.contact_id || selectedDialog?.id) === (d.contact_id || d.id) ? 'rgba(124,77,255,0.1)' : 'transparent',
              transition: 'background 0.12s',
            }}
            onMouseEnter={e => e.currentTarget.style.background = 'rgba(255,255,255,0.03)'}
            onMouseLeave={e => e.currentTarget.style.background = (selectedDialog?.contact_id || selectedDialog?.id) === (d.contact_id || d.id) ? 'rgba(124,77,255,0.1)' : 'transparent'}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
                <span style={{ fontWeight: 600, fontSize: 13 }}>{d.name || d.contact_name || 'Контакт'}</span>
                {d.unread_count > 0 && (
                  <span style={{
                    padding: '2px 7px', borderRadius: 10, fontSize: 10, fontWeight: 700,
                    background: 'var(--violet)', color: '#fff',
                  }}>{d.unread_count}</span>
                )}
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {d.last_message || d.preview || '...'}
              </div>
              <div style={{ display: 'flex', gap: 6, marginTop: 4 }}>
                {d.is_ai_active && <Badge color="violet">ИИ</Badge>}
                {d.time && <span style={{ fontSize: 10, color: 'var(--text-3)' }}>{new Date(d.time).toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit' })}</span>}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ── Messages (right) ─────────────────────────────── */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
        {/* Chat header */}
        <div style={{
          padding: '14px 20px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div>
            <div style={{ fontWeight: 700, fontSize: 14, letterSpacing: '-0.02em' }}>
              {selectedDialog ? (selectedDialog.name || selectedDialog.contact_name || 'Чат') : 'Выберите диалог'}
            </div>
            {selectedDialog && selectedAccount && (
              <div style={{ fontSize: 11, color: 'var(--text-3)' }}>от {selectedAccount.first_name || selectedAccount.phone}</div>
            )}
          </div>
          {selectedDialog && (
            <Button variant="outline" size="sm" onClick={openAIConfig}>
              🤖 Настроить ИИ
            </Button>
          )}
        </div>

        {/* Messages area */}
        <div style={{ flex: 1, overflow: 'auto', padding: '16px 20px' }}>
          {!selectedDialog ? (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
              <Empty icon="💬" title="Выберите диалог" subtitle="Сообщения появятся здесь" />
            </div>
          ) : msgsLoading ? (
            <div style={{ display: 'flex', justifyContent: 'center', padding: 40 }}><Spinner size={24} /></div>
          ) : messages.length === 0 ? (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
              <Empty icon="📭" title="Нет сообщений" subtitle="История переписки пуста" />
            </div>
          ) : (
            <>
              {messages.map((m, i) => {
                const isMe = m.from === 'me' || m.is_outgoing
                return (
                  <div key={i} style={{
                    display: 'flex', justifyContent: isMe ? 'flex-end' : 'flex-start',
                    marginBottom: 8,
                  }}>
                    <div style={{
                      maxWidth: '70%', padding: '10px 14px', borderRadius: 14,
                      background: isMe
                        ? 'linear-gradient(135deg, rgba(124,77,255,0.25), rgba(61,139,255,0.15))'
                        : 'var(--bg-3)',
                      border: `1px solid ${isMe ? 'rgba(124,77,255,0.2)' : 'var(--border)'}`,
                    }}>
                      <div style={{ fontSize: 13, lineHeight: 1.5, wordBreak: 'break-word' }}>{m.text || m.message}</div>
                      <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 4 }}>
                        {m.is_ai && <Badge color="violet">ИИ</Badge>}
                        <span style={{ fontSize: 10, color: 'var(--text-3)' }}>
                          {m.time ? new Date(m.time).toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit' }) : ''}
                        </span>
                      </div>
                    </div>
                  </div>
                )
              })}
              <div ref={msgsEndRef} />
            </>
          )}
        </div>

        {/* Message input */}
        {selectedDialog && (
          <div style={{
            padding: '12px 20px', borderTop: '1px solid var(--border)',
            display: 'flex', gap: 10, alignItems: 'center',
          }}>
            <input
              value={messageText}
              onChange={e => setMessageText(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() } }}
              placeholder="Написать сообщение... (ручное вмешательство оператора)"
              style={{
                flex: 1, padding: '12px 16px', background: 'var(--bg-3)',
                border: '1px solid var(--border)', borderRadius: 12,
                color: 'var(--text)', fontSize: 13, outline: 'none',
                transition: 'border-color 0.15s',
              }}
              onFocus={e => e.target.style.borderColor = 'var(--violet)'}
              onBlur={e => e.target.style.borderColor = 'var(--border)'}
            />
            <Button variant="primary" onClick={handleSend} loading={sending} disabled={!messageText.trim()}>
              Отправить
            </Button>
          </div>
        )}
      </div>

      {/* ── AI Config Modal ──────────────────────────────── */}
      <Modal open={aiModal} onClose={() => setAiModal(false)} title="Настройка ИИ-диалога" width={520}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ padding: '12px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
            ИИ будет вести переписку от имени аккаунта по заданному системному промпту.
            Вы можете вмешаться вручную в любой момент.
          </div>

          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Системный промпт</label>
            <textarea
              value={aiPrompt}
              onChange={e => setAiPrompt(e.target.value)}
              placeholder={"Ты — менеджер по продажам. Отвечай вежливо, предлагай товар, отвечай на вопросы.\n\nТон: дружелюбный, профессиональный."}
              rows={6}
              style={{
                width: '100%', background: 'var(--bg-3)', border: '1px solid var(--border)',
                borderRadius: 10, color: 'var(--text)', padding: '12px 14px', fontSize: 13,
                fontFamily: 'var(--font-mono)', resize: 'vertical', outline: 'none',
                lineHeight: 1.6,
              }}
            />
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <button
              onClick={() => setAiActive(!aiActive)}
              style={{
                width: 44, height: 24, borderRadius: 12, border: 'none', cursor: 'pointer',
                background: aiActive ? 'var(--green)' : 'var(--bg-4)',
                position: 'relative', transition: 'background 0.2s',
              }}
            >
              <div style={{
                width: 18, height: 18, borderRadius: '50%', background: '#fff',
                position: 'absolute', top: 3,
                left: aiActive ? 23 : 3, transition: 'left 0.2s',
              }} />
            </button>
            <span style={{ fontSize: 13, color: aiActive ? 'var(--green)' : 'var(--text-3)' }}>
              {aiActive ? 'ИИ-диалог активен' : 'ИИ-диалог выключен'}
            </span>
          </div>

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
            <Button variant="ghost" onClick={() => setAiModal(false)}>Отмена</Button>
            <Button variant="primary" onClick={saveAIConfig} loading={aiSaving}>Сохранить</Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
