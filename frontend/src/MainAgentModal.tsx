import { useState } from 'react';
import { LoaderCircle } from 'lucide-react';
import { Modal } from './components';
import { put, errorText } from './api';
import type { Project } from './types';

export default function MainAgentModal({
  project,
  onClose,
  onSave,
}: {
  project: Project;
  onClose: () => void;
  onSave: (project: Project) => void;
}) {
  const [profile, setProfile] = useState(project.main_agent);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      onSave(
        await put<Project>(`/projects/${project.id}/main-agent`, {
          name: profile.name.trim(),
          instructions: profile.instructions,
          model: profile.model.trim(),
        }),
      );
      onClose();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Your main agent" subtitle={project.name} onClose={onClose}>
      <form onSubmit={save}>
        <div className="settings-body">
          <label>
            Name
            <input
              required
              maxLength={60}
              value={profile.name}
              onChange={(e) => setProfile({ ...profile, name: e.target.value })}
            />
          </label>
          <label>
            Standing instructions
            <textarea
              rows={6}
              value={profile.instructions}
              onChange={(e) => setProfile({ ...profile, instructions: e.target.value })}
              placeholder="How should this agent work with you? Project conventions, priorities, communication style…"
            />
          </label>
          <label>
            Model
            <input
              value={profile.model}
              onChange={(e) => setProfile({ ...profile, model: e.target.value })}
              placeholder="Use the model in Settings"
              spellCheck={false}
            />
          </label>
          <p className="field-help">
            Only for this repository. The main agent and its team use this model. Changes apply to
            new assignments; work already running keeps its settings.
          </p>
          {error && (
            <div className="form-error" role="alert">
              {error}
            </div>
          )}
        </div>
        <div className="modal-footer">
          <button type="button" className="button secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="button primary" disabled={busy || !profile.name.trim()}>
            {busy && <LoaderCircle size={14} className="spin" />}Save agent
          </button>
        </div>
      </form>
    </Modal>
  );
}
