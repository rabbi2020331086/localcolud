import type {ButtonHTMLAttributes,ReactNode} from 'react';
export function Button({className='',...props}:ButtonHTMLAttributes<HTMLButtonElement>){return <button className={`button ${className}`} {...props}/>}
export function Card({children,className=''}:{children:ReactNode;className?:string}){return <section className={`card ${className}`}>{children}</section>}
export function PageHeader({title,description,action}:{title:string;description?:string;action?:ReactNode}){return <header className="page-header"><div><h1>{title}</h1>{description&&<p>{description}</p>}</div>{action}</header>}
const variants:Record<string,string>={RUNNING:'success',ONLINE:'success',SUCCEEDED:'success',STOPPED:'neutral',OFFLINE:'neutral',HIBERNATED:'neutral',FAILED:'danger',ERROR:'danger',PENDING:'info',PROVISIONING:'info',STARTING:'info',STOPPING:'warning',DRAINING:'warning',HIBERNATING:'warning',TERMINATING:'warning'};
export function Status({value}:{value:string}){return <span className={`status ${variants[value]??'neutral'}`}><i/>{value.replaceAll('_',' ')}</span>}
export function Empty({title,detail}:{title:string;detail:string}){return <div className="empty"><h3>{title}</h3><p>{detail}</p></div>}
export function ErrorAlert({children}:{children:ReactNode}){return <div className="alert error" role="alert">{children}</div>}
export function Loading(){return <div className="loading"><span/>Loading…</div>}
