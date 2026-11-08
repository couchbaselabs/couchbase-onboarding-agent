import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { Permission, PermissionDef, RbacRole, RbacUser } from "@/api/types";
import { useAuthSession } from "@/hooks/useAuthSession";

/** Settings -> RBAC & Roles: the users who can sign in and the roles that say
 * what each may do. Built-in roles (admin, operator, read_only) are fixed;
 * custom roles are any subset of the permission catalog. */
export default function RbacSettings() {
  const [users, setUsers] = useState<RbacUser[]>([]);
  const [roles, setRoles] = useState<RbacRole[]>([]);
  const [perms, setPerms] = useState<PermissionDef[]>([]);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const [u, r, p] = await Promise.all([api.listUsers(), api.listRoles(), api.listPermissions()]);
      setUsers(u);
      setRoles(r);
      setPerms(p);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't load users and roles.");
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  return (
    <div className="stack">
      {error && (
        <div className="helper-banner helper-banner-danger" role="alert">
          {error}
        </div>
      )}
      <section className="card">
        <div className="card-title-row">
          <div className="card-title">Users</div>
        </div>
        <p className="field-hint" style={{ marginBottom: 14 }}>
          Local accounts that can log in with a password. Each user has exactly one role. Disabling or deleting a user
          signs them out immediately; so does resetting their password.
        </p>
        <UsersTable users={users} roles={roles} onChange={reload} />
        <AddUser roles={roles} onDone={reload} />
      </section>

      <section className="card">
        <div className="card-title-row">
          <div className="card-title">Roles</div>
        </div>
        <p className="field-hint" style={{ marginBottom: 14 }}>
          A role is a set of permissions. <strong>admin</strong>, <strong>operator</strong> and{" "}
          <strong>read_only</strong> are built in and can't be changed; create a custom role for anything in
          between. Changes to a custom role apply to its members on their next request.
        </p>
        <RolesTable roles={roles} perms={perms} onChange={reload} />
        <RoleEditor perms={perms} onDone={reload} />
      </section>
    </div>
  );
}

// ---------------------------------------------------------------- users

function UsersTable({ users, roles, onChange }: { users: RbacUser[]; roles: RbacRole[]; onChange: () => void }) {
  const { session } = useAuthSession();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resetting, setResetting] = useState<string | null>(null);

  async function run(username: string, fn: () => Promise<unknown>) {
    setBusy(username);
    setError(null);
    try {
      await fn();
      onChange();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That change didn't go through.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      {error && (
        <div className="helper-banner helper-banner-danger" role="alert" style={{ marginBottom: 10 }}>
          {error}
        </div>
      )}
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Username</th>
              <th>Role</th>
              <th>Status</th>
              <th>Password set</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const me = session?.username === u.username;
              return (
                <tr key={u.username}>
                  <td>
                    <strong>{u.username}</strong>
                    {me && <span className="badge badge-info" style={{ marginLeft: 8 }}>you</span>}
                  </td>
                  <td>
                    <select
                      value={u.role}
                      disabled={busy === u.username}
                      onChange={(e) => run(u.username, () => api.updateUser(u.username, { role: e.target.value }))}
                      aria-label={`Role for ${u.username}`}
                    >
                      {roles.map((r) => (
                        <option key={r.name} value={r.name}>
                          {r.name}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    {u.disabled ? (
                      <span className="badge badge-danger">disabled</span>
                    ) : (
                      <span className="badge badge-success">active</span>
                    )}
                  </td>
                  <td className="field-hint">{new Date(u.password_set_at).toLocaleDateString()}</td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={busy === u.username}
                      onClick={() => setResetting(resetting === u.username ? null : u.username)}
                    >
                      Reset password
                    </button>{" "}
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={busy === u.username || me}
                      title={me ? "You can't disable your own account" : undefined}
                      onClick={() => run(u.username, () => api.updateUser(u.username, { disabled: !u.disabled }))}
                    >
                      {u.disabled ? "Enable" : "Disable"}
                    </button>{" "}
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={busy === u.username || me}
                      title={me ? "You can't delete your own account" : undefined}
                      onClick={() => {
                        if (window.confirm(`Delete user "${u.username}"? This can't be undone.`))
                          run(u.username, () => api.deleteUser(u.username));
                      }}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {resetting && (
        <ResetPassword
          username={resetting}
          onDone={() => {
            setResetting(null);
            onChange();
          }}
          onCancel={() => setResetting(null)}
        />
      )}
    </>
  );
}

function ResetPassword({ username, onDone, onCancel }: { username: string; onDone: () => void; onCancel: () => void }) {
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <form
      className="form-grid"
      style={{ marginTop: 12 }}
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError(null);
        try {
          await api.updateUser(username, { password: pw });
          onDone();
        } catch (err) {
          setError(err instanceof ApiError ? err.message : "Couldn't reset the password.");
        } finally {
          setBusy(false);
        }
      }}
    >
      <div className="field">
        <label htmlFor="reset-pw">
          New password for <strong>{username}</strong>
        </label>
        <input id="reset-pw" type="password" autoComplete="new-password" autoFocus value={pw} onChange={(e) => setPw(e.target.value)} />
        <span className="field-hint">At least 12 characters. Their other sessions are signed out.</span>
      </div>
      {error && (
        <div className="helper-banner helper-banner-danger" role="alert">
          {error}
        </div>
      )}
      <div className="btn-wrap">
        <button className="btn btn-primary" type="submit" disabled={busy || pw.length < 12}>
          {busy ? "Saving..." : "Set password"}
        </button>
        <button className="btn btn-secondary" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function AddUser({ roles, onDone }: { roles: RbacRole[]; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("read_only");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open)
    return (
      <div className="btn-wrap" style={{ marginTop: 12 }}>
        <button className="btn btn-primary" type="button" onClick={() => setOpen(true)}>
          Add user
        </button>
      </div>
    );

  return (
    <form
      className="form-grid"
      style={{ marginTop: 14 }}
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError(null);
        try {
          await api.createUser({ username: username.trim().toLowerCase(), password, role });
          setUsername("");
          setPassword("");
          setOpen(false);
          onDone();
        } catch (err) {
          setError(err instanceof ApiError ? err.message : "Couldn't create the user.");
        } finally {
          setBusy(false);
        }
      }}
    >
      <div className="card-title" style={{ fontSize: 13 }}>
        New user
      </div>
      <div className="field">
        <label htmlFor="nu-name">Username</label>
        <input id="nu-name" autoFocus autoCapitalize="none" spellCheck={false} value={username} onChange={(e) => setUsername(e.target.value)} />
        <span className="field-hint">2-32 characters: lowercase letters, digits, '.', '_' or '-'.</span>
      </div>
      <div className="field">
        <label htmlFor="nu-pw">Initial password</label>
        <input id="nu-pw" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        <span className="field-hint">At least 12 characters. They can change it under Settings → Account.</span>
      </div>
      <div className="field">
        <label htmlFor="nu-role">Role</label>
        <select id="nu-role" value={role} onChange={(e) => setRole(e.target.value)}>
          {roles.map((r) => (
            <option key={r.name} value={r.name}>
              {r.name}
              {r.description ? ` -- ${r.description}` : ""}
            </option>
          ))}
        </select>
      </div>
      {error && (
        <div className="helper-banner helper-banner-danger" role="alert">
          {error}
        </div>
      )}
      <div className="btn-wrap">
        <button className="btn btn-primary" type="submit" disabled={busy || !username.trim() || password.length < 12}>
          {busy ? "Creating..." : "Create user"}
        </button>
        <button className="btn btn-secondary" type="button" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------- roles

function RolesTable({ roles, perms, onChange }: { roles: RbacRole[]; perms: PermissionDef[]; onChange: () => void }) {
  const [editing, setEditing] = useState<RbacRole | null>(null);
  const [error, setError] = useState<string | null>(null);

  return (
    <>
      {error && (
        <div className="helper-banner helper-banner-danger" role="alert" style={{ marginBottom: 10 }}>
          {error}
        </div>
      )}
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Role</th>
              <th>Permissions</th>
              <th className="num">Users</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {roles.map((r) => (
              <tr key={r.name}>
                <td>
                  <strong>{r.name}</strong>
                  {r.builtin && <span className="badge badge-neutral" style={{ marginLeft: 8 }}>built-in</span>}
                  {r.description && <div className="field-hint">{r.description}</div>}
                </td>
                <td>
                  <div className="perm-chips">
                    {r.permissions.map((p) => (
                      <span key={p} className="tag-chip" title={perms.find((d) => d.name === p)?.description}>
                        {p}
                      </span>
                    ))}
                  </div>
                </td>
                <td className="num">{r.users}</td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  {!r.builtin && (
                    <>
                      <button type="button" className="btn btn-secondary btn-sm" onClick={() => setEditing(r)}>
                        Edit
                      </button>{" "}
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        disabled={r.users > 0}
                        title={r.users > 0 ? "Reassign its users first" : undefined}
                        onClick={async () => {
                          if (!window.confirm(`Delete role "${r.name}"?`)) return;
                          try {
                            await api.deleteRole(r.name);
                            onChange();
                          } catch (err) {
                            setError(err instanceof ApiError ? err.message : "Couldn't delete the role.");
                          }
                        }}
                      >
                        Delete
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {editing && (
        <RoleEditor
          key={editing.name}
          perms={perms}
          existing={editing}
          onDone={() => {
            setEditing(null);
            onChange();
          }}
          onCancel={() => setEditing(null)}
        />
      )}
    </>
  );
}

function RoleEditor({
  perms,
  existing,
  onDone,
  onCancel,
}: {
  perms: PermissionDef[];
  existing?: RbacRole;
  onDone: () => void;
  onCancel?: () => void;
}) {
  const [open, setOpen] = useState(!!existing);
  const [name, setName] = useState(existing?.name ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [selected, setSelected] = useState<Set<Permission>>(new Set(existing?.permissions ?? ["migrations:read"]));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open)
    return (
      <div className="btn-wrap" style={{ marginTop: 12 }}>
        <button className="btn btn-primary" type="button" onClick={() => setOpen(true)}>
          Create role
        </button>
      </div>
    );

  function toggle(p: Permission) {
    const next = new Set(selected);
    if (next.has(p)) next.delete(p);
    else next.add(p);
    setSelected(next);
  }

  return (
    <form
      className="form-grid"
      style={{ marginTop: 14 }}
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError(null);
        try {
          const permissions = Array.from(selected);
          if (existing) await api.updateRole(existing.name, { description, permissions });
          else await api.createRole({ name: name.trim().toLowerCase(), description, permissions });
          setOpen(false);
          onDone();
        } catch (err) {
          setError(err instanceof ApiError ? err.message : "Couldn't save the role.");
        } finally {
          setBusy(false);
        }
      }}
    >
      <div className="card-title" style={{ fontSize: 13 }}>
        {existing ? `Edit role "${existing.name}"` : "New role"}
      </div>
      {!existing && (
        <div className="field">
          <label htmlFor="role-name">Name</label>
          <input id="role-name" autoFocus autoCapitalize="none" spellCheck={false} value={name} onChange={(e) => setName(e.target.value)} />
          <span className="field-hint">2-32 characters: lowercase letters, digits, '_' or '-'.</span>
        </div>
      )}
      <div className="field">
        <label htmlFor="role-desc">Description</label>
        <input id="role-desc" value={description} maxLength={200} onChange={(e) => setDescription(e.target.value)} />
      </div>
      <div className="field">
        <span className="field-label">Permissions</span>
        {perms.map((p) => (
          <label key={p.name} className="checkbox-row">
            <input type="checkbox" checked={selected.has(p.name)} onChange={() => toggle(p.name)} />
            <span>
              <code>{p.name}</code>
              <span className="field-hint" style={{ display: "block" }}>
                {p.description}
              </span>
            </span>
          </label>
        ))}
      </div>
      {error && (
        <div className="helper-banner helper-banner-danger" role="alert">
          {error}
        </div>
      )}
      <div className="btn-wrap">
        <button className="btn btn-primary" type="submit" disabled={busy || (!existing && !name.trim()) || selected.size === 0}>
          {busy ? "Saving..." : existing ? "Save role" : "Create role"}
        </button>
        <button
          className="btn btn-secondary"
          type="button"
          onClick={() => {
            setOpen(false);
            onCancel?.();
          }}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
