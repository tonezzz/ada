import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import MobileApp from './MobileApp.jsx'
import IPhoneApp from './IPhoneApp.jsx'
import './index.css'

const isMobileDevice = () => {
    if (typeof navigator === 'undefined') return false;
    const ua = navigator.userAgent || '';
    const isIOS = /iPhone|iPad|iPod/i.test(ua) || (ua.includes('Mac') && navigator.maxTouchPoints > 1);
    const isAndroid = /Android/i.test(ua);
    return isIOS || isAndroid;
};

ReactDOM.createRoot(document.getElementById('root')).render(
    <React.StrictMode>
        {window.location.hash === '#/mobile'
            ? <MobileApp />
            : window.location.hash === '#/iphone'
                ? <IPhoneApp />
                : <App />}
    </React.StrictMode>,
)
