import { useState } from 'react';
import { FolderGit2, LoaderCircle } from 'lucide-react';
import { Modal } from './components';
import { post, errorText } from './api';
import type { Project } from './types';

export default function ProjectModal({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (p: Project) => void;
}) {
  const [name, setName] = useState(''),
    [path, setPath] = useState(''),
    [description, setDescription] = useState(''),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const p = await post<Project>('/projects', {
        name: name.trim(),
        path: path.trim(),
        description: description.trim(),
      });
      onCreate(p);
      onClose();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title="Connect a repository"
      subtitle="Connect a local repository."
      onClose={onClose}
    >
      <form onSubmit={submit}>
        <div className="settings-body">
          <div className="project-modal-icon">
            <FolderGit2 size={26} />
          </div>
          <label>
            Project name
            <input
              autoFocus
              required
              placeholder="My open-source project"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            Repository path
            <input
              required
              placeholder="D:\projects\my-repository"
              spellCheck={false}
              value={path}
              onChange={(e) => {
                setPath(e.target.value);
                if (!name)
                  setName(
                    e.target.value
                      .replace(/[\\/]+$/, '')
                      .split(/[\\/]/)
                      .pop() || '',
                  );
              }}
            />
          </label>
          <p className="field-help">
            Use the full path to a folder on the machine running EVO. Git repositories get an
            isolated worktree for each task.
          </p>
          <label>
            A little context <span className="optional">optional</span>
            <textarea
              rows={3}
              placeholder="What does this project do? Anything your agents should know?"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
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
          <button
            className="button primary"
            type="submit"
            disabled={busy || !name.trim() || !path.trim()}
          >
            {busy ? <LoaderCircle size={15} className="spin" /> : <FolderGit2 size={15} />}Connect
            project
          </button>
        </div>
      </form>
    </Modal>
  );
}
