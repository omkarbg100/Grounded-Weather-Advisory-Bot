import React, { useState } from 'react';
import { ChevronDown, ChevronUp, Cpu, MapPin, Activity } from 'lucide-react';

export default function WhyPanel({ location, facts, trace, outcome }) {
  const [isOpen, setIsOpen] = useState(false);

  if (!facts && (!trace || trace.length === 0) && !location) {
    return null;
  }

  const factEntries = facts ? Object.entries(facts) : [];

  return (
    <div className="why-panel">
      <div className="why-header" onClick={() => setIsOpen(!isOpen)}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
          <Cpu size={14} color="#38bdf8" />
          <span>Why this answer? (Trace & Facts)</span>
        </div>
        {isOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </div>

      {isOpen && (
        <div className="why-body">
          {location && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', color: '#38bdf8' }}>
              <MapPin size={13} />
              <span>
                Location: <strong>{location.label}</strong> ({location.source} source)
              </span>
            </div>
          )}

          {factEntries.length > 0 && (
            <div>
              <div style={{ color: '#94a3b8', fontSize: '0.75rem', marginBottom: '0.3rem', display: 'flex', alignItems: 'center', gap: '0.3rem' }}>
                <Activity size={12} />
                <span>Derived Weather Facts:</span>
              </div>
              <div className="facts-grid">
                {factEntries.map(([key, val]) => (
                  <div className="fact-item" key={key}>
                    <div className="fact-name">{key}</div>
                    <div className="fact-val">{String(val)}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {trace && trace.length > 0 && (
            <div>
              <div style={{ color: '#94a3b8', fontSize: '0.75rem', marginBottom: '0.3rem' }}>
                Execution Graph Trace:
              </div>
              <div className="trace-steps">
                {trace.map((step, idx) => (
                  <React.Fragment key={idx}>
                    <span className="trace-step">{step}</span>
                    {idx < trace.length - 1 && <span className="trace-arrow">→</span>}
                  </React.Fragment>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
