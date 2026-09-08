import { create } from "zustand";
import { api } from "../api/client";
import { getObject, setObject } from "../api/scope";
import type { AppConfig, ObjectInfo, UserInfo } from "../api/types";

/**
 * Estado que atravessa objetos: config global, quem sou eu, qual objeto abri.
 *
 * Separado de `library.ts` de propósito: aquela store é estado DE UM objeto e é
 * zerada ao trocar de objeto. Guardar o usuário lá o derrubaria junto.
 */
interface SessionState {
  config: AppConfig | null;
  user: UserInfo | null;
  users: UserInfo[];
  objects: ObjectInfo[];
  activeObject: ObjectInfo | null;
  loading: boolean;
  error: string | null;

  boot: () => Promise<void>;
  refreshConfig: () => Promise<void>;
  login: (userId: string) => Promise<void>;
  addUser: (displayName: string) => Promise<UserInfo>;
  logout: () => Promise<void>;
  openObject: (objectId: string) => void;
  closeObject: () => void;
  addObject: (payload: {
    display_name: string;
    label?: string;
    gcs_uri?: string;
  }) => Promise<ObjectInfo>;
}

export const useSession = create<SessionState>((set, get) => ({
  config: null,
  user: null,
  users: [],
  objects: [],
  activeObject: null,
  loading: true,
  error: null,

  boot: async () => {
    set({ loading: true, error: null });
    try {
      const config = await api.config();
      // Retoma o objeto da aba (localStorage), caindo no último usado no
      // servidor. Sempre VALIDADO contra a lista: um objeto renomeado ou
      // removido não pode deixar o app preso numa tela morta pedindo dados de
      // algo que não existe mais.
      const remembered = getObject() ?? config.last_object_id;
      const found = config.objects.find((o) => o.object_id === remembered) ?? null;
      setObject(found ? found.object_id : null);
      set({
        config,
        user: config.user,
        users: config.users,
        objects: config.objects,
        activeObject: found,
        loading: false,
      });
    } catch (error) {
      set({ loading: false, error: (error as Error).message });
    }
  },

  refreshConfig: async () => {
    const config = await api.config();
    set({
      config,
      user: config.user,
      users: config.users,
      objects: config.objects,
    });
  },

  login: async (userId) => {
    const user = await api.login(userId);
    set({ user });
  },

  addUser: async (displayName) => {
    const user = await api.createUser(displayName);
    set((state) => ({ users: [...state.users, user] }));
    return user;
  },

  logout: async () => {
    await api.logout();
    set({ user: null, activeObject: null });
    setObject(null);
  },

  openObject: (objectId) => {
    const found = get().objects.find((o) => o.object_id === objectId);
    if (!found) return;
    setObject(objectId);
    set({ activeObject: found });
  },

  closeObject: () => {
    setObject(null);
    set({ activeObject: null });
  },

  addObject: async (payload) => {
    const created = await api.createObject(payload);
    set((state) => ({ objects: [...state.objects, created] }));
    return created;
  },
}));
