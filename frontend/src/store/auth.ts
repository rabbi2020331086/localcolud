import {create} from 'zustand';import type {User} from '../types';
type State={accessToken:string|null;user:User|null;ready:boolean;setSession:(token:string,user:User)=>void;clear:()=>void;setReady:()=>void};
export const useAuth=create<State>((set)=>({accessToken:null,user:null,ready:false,setSession:(accessToken,user)=>set({accessToken,user,ready:true}),clear:()=>set({accessToken:null,user:null,ready:true}),setReady:()=>set({ready:true})}));

