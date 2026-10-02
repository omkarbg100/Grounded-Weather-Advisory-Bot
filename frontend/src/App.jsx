import React, { useState, useEffect, useRef } from 'react';
import { sendMessage } from './api';
import MessageBubble from './components/MessageBubble';
import { Send, CloudSun, RefreshCw } from 'lucide-react';

function generateSessionId() {
  return `session_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;
}

export default function App() {
  const [sessionId, setSessionId] = useState('');
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const chatEndRef = useRef(null);

  useEffect(() => {
    // Generate session ID once per page load
    const newSessionId = generateSessionId();
    setSessionId(newSessionId);

    setMessages([
      {
        id: 'welcome',
        sender: 'bot',
        text: 'Welcome to the SOP-Grounded Weather Safety Assistant! Ask me if your outdoor activity (cycling, running, picnic, travel, etc.) is safe today.',
      },
    ]);
  }, []);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  const handleSend = async (e) => {
    e?.preventDefault();
    if (!input.trim() || loading) return;

    const userText = input.trim();
    setInput('');

    const userMsg = {
      id: `usr_${Date.now()}`,
      sender: 'user',
      text: userText,
    };

    setMessages((prev) => [...prev, userMsg]);
    setLoading(true);

    try {
      const res = await sendMessage(sessionId, userText);
      const botMsg = {
        id: `bot_${Date.now()}`,
        sender: 'bot',
        text: res.reply,
        outcome: res.outcome,
        sop_ids: res.sop_ids,
        location: res.location,
        facts_used: res.facts_used,
        trace: res.trace,
      };
      setMessages((prev) => [...prev, botMsg]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          id: `err_${Date.now()}`,
          sender: 'bot',
          text: `Error: ${err.message || 'Could not connect to backend server.'}`,
        },
      ]);
    } finally {
      setLoading(false);
    }
  };

  const handleResetSession = () => {
    const newSessionId = generateSessionId();
    setSessionId(newSessionId);
    setMessages([
      {
        id: 'welcome_reset',
        sender: 'bot',
        text: 'Session reset! How can I assist you with weather safety for your outdoor activity?',
      },
    ]);
  };

  return (
    <div className="app-container">
      <header className="header">
        <div className="header-title">
          <CloudSun size={28} color="#38bdf8" />
          <h1>Weather SOP Advisory Bot</h1>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
          <span className="session-badge">Session: {sessionId.slice(0, 16)}...</span>
          <button
            onClick={handleResetSession}
            style={{
              background: 'transparent',
              border: '1px solid rgba(255,255,255,0.1)',
              color: '#94a3b8',
              padding: '0.3rem 0.6rem',
              borderRadius: '0.5rem',
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '0.3rem',
              fontSize: '0.75rem',
            }}
            title="Reset Chat Session"
          >
            <RefreshCw size={12} />
            <span>Reset</span>
          </button>
        </div>
      </header>

      <main className="chat-thread">
        {messages.map((msg) => (
          <MessageBubble key={msg.id} message={msg} />
        ))}
        {loading && (
          <div className="message-row bot">
            <div className="bubble bot" style={{ opacity: 0.7 }}>
              Evaluating weather facts against SOP policies...
            </div>
          </div>
        )}
        <div ref={chatEndRef} />
      </main>

      <form className="input-bar" onSubmit={handleSend}>
        <input
          type="text"
          placeholder="Ask a question (e.g. 'Is it safe to cycle in Bhopal today?')..."
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={loading}
        />
        <button type="submit" disabled={loading || !input.trim()}>
          <Send size={16} />
        </button>
      </form>
    </div>
  );
}
