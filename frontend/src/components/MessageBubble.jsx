import React from 'react';
import WhyPanel from './WhyPanel';
import { ShieldCheck, User } from 'lucide-react';

export default function MessageBubble({ message }) {
  const isUser = message.sender === 'user';

  return (
    <div className={`message-row ${isUser ? 'user' : 'bot'}`}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.75rem', color: '#94a3b8' }}>
        {isUser ? <User size={13} /> : <ShieldCheck size={13} color="#38bdf8" />}
        <span>{isUser ? 'You' : 'Weather Safety Bot'}</span>
      </div>

      <div className={`bubble ${isUser ? 'user' : 'bot'}`}>
        {message.text}

        {!isUser && message.sop_ids && message.sop_ids.length > 0 && (
          <div className="sop-chips">
            {message.sop_ids.map((sopId) => (
              <span className="sop-chip" key={sopId}>
                Cited SOP: {sopId}
              </span>
            ))}
          </div>
        )}
      </div>

      {!isUser && (
        <WhyPanel
          location={message.location}
          facts={message.facts_used}
          trace={message.trace}
          outcome={message.outcome}
        />
      )}
    </div>
  );
}
