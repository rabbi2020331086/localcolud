import axios,{AxiosError,InternalAxiosRequestConfig} from 'axios';import {useAuth} from '../store/auth';import type {User} from '../types';
export type Problem={type:string;title:string;status:number;detail:string;request_id:string};
export const api=axios.create({baseURL:import.meta.env.VITE_API_URL??'http://localhost:8000/api/v1',withCredentials:true,headers:{'Content-Type':'application/json'}});
api.interceptors.request.use((config)=>{const token=useAuth.getState().accessToken;if(token)config.headers.Authorization=`Bearer ${token}`;return config});
let refreshing:Promise<string>|null=null;
api.interceptors.response.use(r=>r,async(error:AxiosError<Problem>)=>{const original=error.config as (InternalAxiosRequestConfig&{_retried?:boolean})|undefined;if(error.response?.status===401&&original&&!original._retried&&!original.url?.includes('/auth/refresh')){original._retried=true;refreshing??=api.post<{access_token:string;user:User}>('/auth/refresh').then(({data})=>{useAuth.getState().setSession(data.access_token,data.user);return data.access_token}).finally(()=>{refreshing=null});try{original.headers.Authorization=`Bearer ${await refreshing}`;return api(original)}catch{useAuth.getState().clear()}}return Promise.reject(error)});
export const message=(error:unknown)=>axios.isAxiosError<Problem>(error)?error.response?.data.detail??error.message:'An unexpected error occurred';

