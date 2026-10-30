import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { MantineProvider } from '@mantine/core';
import { mantineTheme } from '../mantineTheme';
import { APPEARANCE_KEY, DEFAULT_APPEARANCE, parseAppearance, readAppearance, saveAppearance, type Appearance } from './appearance';
import styles from './AppearanceBackdrop.module.css';

const AppearanceContext=createContext({ appearance:DEFAULT_APPEARANCE, setAppearance:(_next:Appearance)=>{} });
export function useAppearance(){return useContext(AppearanceContext);}
export default function AppearanceProvider({children}:{children:ReactNode}) {
  const [appearance,setValue]=useState(readAppearance);
  const setAppearance=useCallback((next:Appearance)=>setValue(saveAppearance(next)),[]);
  useEffect(()=>{
    const sync=(event:StorageEvent)=>{if(event.key===APPEARANCE_KEY||event.key===null)setValue(parseAppearance(event.newValue));};
    window.addEventListener('storage',sync);
    return()=>window.removeEventListener('storage',sync);
  },[]);
  useEffect(()=>{
    document.documentElement.style.colorScheme=appearance.scheme;
    document.documentElement.style.backgroundColor=appearance.scheme==='light'?'#ffffff':'#242424';
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content',appearance.scheme==='light'?'#ffffff':'#242424');
  },[appearance.scheme]);
  const theme=useMemo(()=>({...mantineTheme,primaryColor:appearance.accent}),[appearance.accent]);
  const context=useMemo(()=>({appearance,setAppearance}),[appearance,setAppearance]);
  return <AppearanceContext.Provider value={context}><MantineProvider theme={theme} forceColorScheme={appearance.scheme}>{appearance.background!==false&&<div className={styles.backdrop} aria-hidden="true" data-theme-background=""/>}{children}</MantineProvider></AppearanceContext.Provider>;
}
