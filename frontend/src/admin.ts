import { createContext, useContext } from 'react'

/** Whether the signed-in account has the admin role. Admins see the analysis
 *  and generation screens and the scores in the detail pane; normal users see
 *  the radar, the list and the portfolio. The server enforces the same split. */
export const AdminContext = createContext(false)

export const useIsAdmin = () => useContext(AdminContext)
