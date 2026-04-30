import { createContext, useContext } from 'react';

export const EventStreamContext = createContext({ connected: false });
export const useEventStreamStatus = () => useContext(EventStreamContext);
