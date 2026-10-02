const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

export async function sendMessage(sessionId, message) {
  const response = await fetch(`${API_BASE_URL}/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      session_id: sessionId,
      message: message,
    }),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(errorData.detail || 'Failed to send message.');
  }

  return await response.json();
}

export async function fetchSOPs() {
  const response = await fetch(`${API_BASE_URL}/sops`);
  if (!response.ok) {
    throw new Error('Failed to fetch SOP registry.');
  }
  return await response.json();
}
